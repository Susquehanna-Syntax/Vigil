"""A poller: this host checks the devices of its site that cannot run an agent.

Only an agent whose own agent.yml says ``poller: true`` does any of this. The
dashboard hands it a check list in each check-in response, signed the way the
relay list is signed, and the agent runs only a list that verifies. Results go
back in the next check-in. Nothing here blocks the check-in loop: the checks
run on a thread of their own.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime, timezone

from nacl.exceptions import BadSignatureError

from . import netcheck, verify
from .failover import _canonical

logger = logging.getLogger("vigil.poller")

MAX_RESULTS = 500  # queued results, and the most sent in one check-in
KINDS = ("icmp", "tcp", "http", "dns", "snmp")
MIN_INTERVAL = 30
MAX_INTERVAL = 86400
MAX_SLEEP = 30.0
MIN_SLEEP = 1.0
_MAX_LOGGED = 1000  # the once-only log keys are forgotten past this many


class Poller:
    def __init__(self, config, *, run_check=netcheck.run_check,
                 clock=time.monotonic, wall=time.time):
        self._config = config
        self._run_check = run_check
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._checks: dict[str, dict] = {}      # id -> {"kind", "params", "interval"}
        self._next_due: dict[str, float] = {}   # id -> clock() time of the next run
        self._results: list[dict] = []
        self._issued: int | None = None         # issue time of the list in use
        self._logged: set = set()               # log keys already written once

    # ── the check list ───────────────────────────────────────────────────

    def learn(self, response: dict, verify_key) -> None:
        """Adopt the check list the dashboard signed for this agent, if it verifies."""
        if not self._config.poller or verify_key is None or not isinstance(response, dict):
            return
        if not {"checks", "checks_issued", "checks_sig"} <= set(response):
            return
        checks, issued, signature = (response["checks"], response["checks_issued"],
                                     response["checks_sig"])
        if not isinstance(checks, list) or not isinstance(issued, int) \
                or isinstance(issued, bool) or not isinstance(signature, str):
            self._warn_once(("malformed", None), "ignored a malformed check list")
            return
        payload = {
            "v": 1,
            "type": "check-list",
            "agent": verify.agent_fingerprint(self._config.agent_token),
            "issued": issued,
            "checks": checks,
        }
        try:
            verify_key.verify(_canonical(payload), base64.b64decode(signature))
        except (BadSignatureError, ValueError, TypeError):
            self._warn_once(("unsigned", issued), "ignored a check list that does not verify")
            return
        # A genuine list older than the one in use is a replay: a check that was
        # removed could otherwise come back.
        with self._lock:
            if self._issued is not None and issued < self._issued:
                logger.warning("ignored a check list older than the one in use")
                return

        accepted: dict[str, dict] = {}
        dropped = 0
        for entry in checks:
            clean = _clean_check(entry)
            if clean is None or clean[0] in accepted:
                dropped += 1
                continue
            accepted[clean[0]] = clean[1]
        if dropped:
            self._warn_once(("dropped", issued), f"dropped {dropped} malformed device check(s)")

        with self._lock:
            # Re-check under the lock: a newer list may have landed meanwhile.
            if self._issued is not None and issued < self._issued:
                return
            self._issued = issued
            # A check that stays keeps its schedule; a new one is due at once
            # (no entry in _next_due); a removed one stops being run.
            for check_id in list(self._next_due):
                if check_id not in accepted:
                    del self._next_due[check_id]
            self._checks = accepted

    # ── results ──────────────────────────────────────────────────────────

    def take_results(self) -> list[dict]:
        """Drain up to MAX_RESULTS results for the next check-in."""
        with self._lock:
            out = self._results[:MAX_RESULTS]
            del self._results[:MAX_RESULTS]
            return out

    def restore_results(self, results: list[dict]) -> None:
        """A check-in failed: put these back at the front, keeping the newest."""
        if not results:
            return
        with self._lock:
            combined = list(results) + self._results
            self._results = combined[-MAX_RESULTS:]

    # ── scheduling ───────────────────────────────────────────────────────

    def run_due(self) -> int:
        """One scheduler pass: run every due check, return how many ran."""
        now = self._clock()
        with self._lock:
            due = [(check_id, dict(spec)) for check_id, spec in self._checks.items()
                   if check_id not in self._next_due or now >= self._next_due[check_id]]
        ran = 0
        for check_id, spec in due:
            check = {**spec["params"], "kind": spec["kind"]}
            try:
                raw = self._run_check(check)
            except Exception as exc:  # noqa: BLE001 - one bad check must not stop the pass
                raw = {"ok": False, "latency_ms": None,
                       "detail": str(exc) or type(exc).__name__}
            if not isinstance(raw, dict):
                raw = {"ok": False, "latency_ms": None, "detail": "check failed unexpectedly"}
            at = datetime.fromtimestamp(self._wall(), tz=timezone.utc).isoformat()
            # The server already knows the target and the kind of each check.
            body = {k: v for k, v in raw.items() if k not in ("target", "kind", "check", "at")}
            result = {"check": check_id, "at": at, **body}
            with self._lock:
                if check_id in self._checks:
                    self._next_due[check_id] = self._clock() + spec["interval"]
                    self._results.append(result)
                    if len(self._results) > MAX_RESULTS:
                        del self._results[:-MAX_RESULTS]
            ran += 1
        return ran

    def start(self, should_stop) -> None:
        """Run checks on a daemon thread until should_stop() is true."""
        thread = threading.Thread(target=self._loop, args=(should_stop,),
                                  name="vigil-poller", daemon=True)
        thread.start()

    def _loop(self, should_stop) -> None:
        while True:
            delay = MAX_SLEEP
            try:
                if should_stop():
                    return
                self.run_due()
                delay = self._next_delay()
            except Exception:  # noqa: BLE001 - the thread must survive any pass
                logger.exception("device check pass failed")
            time.sleep(delay)

    def _next_delay(self) -> float:
        with self._lock:
            if not self._checks:
                return MAX_SLEEP
            now = self._clock()
            earliest = min(self._next_due.get(check_id, now) for check_id in self._checks)
        return min(MAX_SLEEP, max(MIN_SLEEP, earliest - now))

    # ── logging ──────────────────────────────────────────────────────────

    def _warn_once(self, key, message: str) -> None:
        with self._lock:
            if key in self._logged:
                return
            if len(self._logged) >= _MAX_LOGGED:
                self._logged.clear()
            self._logged.add(key)
        logger.warning(message)


def _clean_check(entry) -> tuple[str, dict] | None:
    """(id, spec) for a well-formed entry, None for anything else."""
    if not isinstance(entry, dict):
        return None
    check_id = entry.get("id")
    kind = entry.get("kind")
    params = entry.get("params")
    interval = entry.get("interval")
    if not isinstance(check_id, str):
        return None
    if kind not in KINDS or not isinstance(params, dict):
        return None
    if isinstance(interval, bool) or not isinstance(interval, int) \
            or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        return None
    return check_id, {"kind": kind, "params": params, "interval": interval}
