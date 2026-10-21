"""Failover: an agent moving to another connection point when its primary dies.

The agent's ``server_url`` is its **primary** — a relay or the dashboard itself.
Alongside it sits an ordered list of **fallbacks**: the ones written into
``agent.yml`` by hand or by the installer, and the ones the dashboard hands out
at check-in (see ``apps.relays.models.signed_relay_list``). When the primary
stops answering, this module walks that list inside a single check-in pass and
leaves the agent parked on whatever answered. Every ``FAILBACK_SECONDS`` it
tries the primary again.

The learned list arrives in a check-in response that a relay carries, so it is
signed by the dashboard and verified here against the agent's pinned key before
a single address is trusted: a relay, or anyone between the agent and the
dashboard, cannot steer agents to an address of their choosing. A failed
signature is dropped whole, with one warning.

All state lives in ``data_dir/endpoints.json`` so a restart after an outage does
not send the agent back to the dead primary.
"""

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import requests
from nacl.exceptions import BadSignatureError

from . import verify

logger = logging.getLogger("vigil.failover")

FAILBACK_SECONDS = 600
#: Consecutive "queued" answers before trying the next address. One pass may
#: reach an address that queues three times in a row — the answer is still the
#: payload it queued, so the counter lives on this object across passes.
QUEUED_LIMIT = 3
STATE_FILE = "endpoints.json"

#: Statuses that mean "this relay is down", not "this agent is refused".
_GATEWAY_STATUSES = frozenset({502, 503, 504})

_MAX_LEARNED = 10


class Endpoints:
    """The primary, the configured fallbacks, and what the dashboard told us.

    ``config.server_url`` is this object's output: every request the agent makes
    reads it, so switching the active address redirects check-ins, task results,
    downloads and the wake thread together.
    """

    def __init__(self, config, now: Callable[[], float] = time.monotonic):
        self._config = config
        self._now = now
        self.primary = config.primary_url or config.server_url
        self.configured = list(config.fallback_urls)
        self.learned: list[str] = []
        self.active = self.primary
        self._last_failover: float | None = None
        self._queued = 0
        self._path = Path(config.data_dir) / STATE_FILE
        self._load()
        self._config.server_url = self.active

    # ── state file ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            logger.warning("Could not read %s — starting from the primary", self._path)
            return
        if not isinstance(raw, dict):
            return
        learned = raw.get("learned")
        if isinstance(learned, list):
            self.learned = [u for u in learned if isinstance(u, str) and _is_http(u)]
        active = raw.get("active")
        # An address removed from agent.yml since it was saved must not keep
        # the agent pointed somewhere its operator took away.
        if isinstance(active, str) and active in self.candidates():
            self.active = active

    def _save(self) -> None:
        state = {"active": self.active, "learned": self.learned}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.parent / (self._path.name + ".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except OSError:
            logger.warning("Could not save %s", self._path)

    # ── selection ────────────────────────────────────────────────────────

    def candidates(self) -> list[str]:
        return _dedupe([self.primary, *self.configured, *self.learned])

    def order_from_active(self) -> list[str]:
        candidates = self.candidates()
        try:
            index = candidates.index(self.active)
        except ValueError:
            index = 0
        return candidates[index:] + candidates[:index]

    def use(self, url: str) -> None:
        if url == self.active:
            return
        left_primary = self.active == self.primary
        self.active = url
        self._config.server_url = url
        # The clock starts when the agent leaves the primary and stops when it
        # gets back. A pass that hops fallback to fallback must not postpone
        # fail-back with every hop.
        self._last_failover = None if url == self.primary else (
            self._last_failover if not left_primary else self._now())
        logger.info("connected through %s", url)
        self._save()

    def learn(self, response, verify_key) -> None:
        """Store the relay list the dashboard signed for this agent.

        A payload whose keys and values are the right shape is verified before
        any URL is looked at; "unsigned" here covers a tampered field as well as
        a missing signature. One that never had the keys at all — an older
        server, or a relay that strips them — is simply nothing to learn.
        """
        if verify_key is None or not isinstance(response, dict):
            return
        if not {"relays", "direct", "relays_sig"} <= set(response):
            return
        relays, direct, signature = (response["relays"], response["direct"],
                                     response["relays_sig"])
        if not isinstance(relays, list) or not isinstance(direct, bool) \
                or not isinstance(signature, str):
            self._ignored()
            return
        payload = {
            "v": 1,
            "agent": verify.agent_fingerprint(self._config.agent_token),
            "relays": relays,
            "direct": direct,
        }
        try:
            verify_key.verify(_canonical(payload), base64.b64decode(signature))
        except (BadSignatureError, ValueError, TypeError):
            self._ignored()
            return
        # A valid signature is still not a licence to name a non-HTTP address
        # the agent would POST its token to.
        for url in relays:
            if not isinstance(url, str) or not _is_http(url):
                self._ignored()
                return
        # Same rule as fallback_urls: an https deployment never downgrades.
        if self.primary.startswith("https://"):
            relays = [u for u in relays if u.startswith("https://")]
        self.learned = list(relays[:_MAX_LEARNED])
        self._save()

    def failback_due(self) -> bool:
        if self.active == self.primary or self._last_failover is None:
            return False
        return self._now() - self._last_failover >= FAILBACK_SECONDS

    def _ignored(self) -> None:
        logger.warning("ignored an unsigned relay list")


def checkin(endpoints: Endpoints, call: Callable[[], dict]) -> dict:
    """One check-in, across every address if it comes to that.

    *call* makes the request against ``config.server_url``, which this function
    sets before each attempt.
    """
    order = endpoints.order_from_active()
    if endpoints.failback_due():
        order = [endpoints.primary] + [u for u in order if u != endpoints.primary]

    previous = endpoints.active
    last_error: Exception | None = None
    queued_response: dict | None = None
    queued_url: str | None = None

    for url in order:
        endpoints.use(url)
        try:
            response = call()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            _did_not_answer(url, type(exc).__name__)
            continue
        except requests.HTTPError as exc:
            if _status_of(exc) not in _GATEWAY_STATUSES:
                raise
            last_error = exc
            _did_not_answer(url, f"HTTP {_status_of(exc)}")
            continue

        if response.get("status") == "queued":
            endpoints._queued += 1
            if endpoints._queued >= QUEUED_LIMIT:
                # Reachable, but it has no live link to carry this host's
                # tasks. That is its own kind of "not answering": hold the
                # payload we were given and try elsewhere in this pass.
                queued_response, queued_url = response, url
                continue
            return response

        endpoints._queued = 0
        if previous != endpoints.primary and endpoints.active == endpoints.primary:
            logger.info("back on the primary %s", endpoints.primary)
        return response

    if queued_response is not None:
        # Nothing else took the check-in either, so the wait stands. Start the
        # count over: those waits are paid for, and the next pass gets its own
        # three before it goes looking elsewhere. Stay on the address that
        # answered, not the last one that didn't.
        endpoints._queued = 0
        endpoints.use(queued_url)
        return queued_response

    if last_error is not None:
        endpoints.use(previous)
        raise last_error
    return {"status": "queued", "tasks": []}


def _is_http(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True).encode()


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _status_of(exc: requests.HTTPError):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _did_not_answer(url: str, reason: str) -> None:
    logger.warning("%s did not answer (%s), trying the next address", url, reason)
