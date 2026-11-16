"""Keeping a self-update from costing the host its agent (sprint M8).

An update replaces the one program that could report that the update went
wrong, so the checking has to happen around the swap, on the host:

1. **Before the swap, the new build proves itself here.** The running agent
   starts the downloaded build as ``vigil-agent preflight`` with its own
   config. That process loads every module, runs a real collection, and makes
   one authenticated round trip to the server. A build that cannot start on
   this machine (wrong architecture, an older C library, a bundle missing a
   module) or cannot reach the server with this config is never installed, and
   the task fails with the reason while the old agent keeps running.

2. **After the swap, the new agent is on probation.** The old binary is kept
   beside the new one and a marker in the data directory records the update.
   The first check-in that succeeds confirms it: the old binary is deleted and
   the server is told. If check-ins keep failing instead, the new agent puts
   the old binary back, restarts into it, and the old agent tells the server
   the update was rolled back and why.

The swap-back is for the one-file builds (Linux, macOS). A Windows install is
a folder its own running service cannot replace, so there the proof before the
swap is the protection, and the marker still reports the outcome.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .procenv import clean_env

logger = logging.getLogger("vigil.update")

MARKER_NAME = "agent-update.json"
#: The new build unpacks itself, imports everything and talks to the server.
PREFLIGHT_SECONDS = 120
#: Probation ends in a rollback after this many failed check-ins in a row,
#: once the new agent has also been up this long. Both, so one slow start or a
#: server restart during the update is not mistaken for a broken build.
ROLLBACK_AFTER_FAILURES = 3
ROLLBACK_AFTER_SECONDS = 300
#: A reason is shown to an operator; keep it one line they can read.
MAX_REASON = 300

PENDING, CONFIRMED, ROLLED_BACK = "pending", "confirmed", "rolled_back"
#: The agent that came back is the one that was there before: the swap did
#: not happen (the Windows helper could not move the folder, for example).
NOT_APPLIED = "not_applied"


# ── the candidate's side: `vigil-agent preflight` ────────────────────────────


def preflight_main(argv: list[str], config_path: Path | None = None) -> int:
    """Prove this build works on this host with this config. Prints one JSON
    line; exits 0 only when everything did."""
    import argparse

    parser = argparse.ArgumentParser(prog="vigil-agent preflight")
    parser.add_argument("-c", "--config", type=Path, default=config_path)
    args = parser.parse_args(argv)
    try:
        detail = _prove(args.config)
    except Exception as exc:
        print(json.dumps({"ok": False, "version": __version__,
                          "error": _one_line(f"{type(exc).__name__}: {exc}")}))
        return 1
    print(json.dumps({"ok": True, "version": __version__, **detail}))
    return 0


def _prove(config_path: Path | None) -> dict:
    # Every module the agent can reach, imported now: a bundle that lost one
    # fails here, in a process nobody depends on yet.
    from . import (  # noqa: F401
        client,
        collector,
        executor,
        failover,
        netmap,
        sessions,
        software,
        verify,
    )
    from .config import load_config

    config = load_config(config_path)
    metrics = collector.collect_all(config)
    if not metrics:
        raise RuntimeError("the collectors returned nothing")
    # Registering a token the server already knows changes nothing there and
    # answers with the host's id: it proves the address, the certificate and
    # the token without draining a task or writing a metric.
    answer = client.register(config)
    if not answer.get("id"):
        raise RuntimeError("the server did not recognise this agent")
    return {"metrics": len(metrics), "host_status": answer.get("status", "")}


# ── the running agent's side ─────────────────────────────────────────────────


def run_preflight(candidate: Path, config) -> str:
    """Run *candidate* as ``preflight``. Returns the version it says it is
    when it proved itself; raises ``ValueError`` with the reason when it did
    not. A candidate from before this command existed cannot be asked, and is
    let through with no version."""
    argv = [str(candidate), "preflight"]
    if getattr(config, "config_path", None):
        argv += ["-c", str(config.config_path)]
    try:
        # The candidate is another one-file bundle. Told to reset, its loader
        # unpacks its own contents instead of adopting this process's.
        done = subprocess.run(argv, capture_output=True, text=True,
                              env=clean_env({"PYINSTALLER_RESET_ENVIRONMENT": "1"}),
                              timeout=PREFLIGHT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ValueError(f"the new agent did not finish its self-check in "
                         f"{PREFLIGHT_SECONDS} seconds; the update was not installed")
    except OSError as exc:
        raise ValueError(f"the new agent cannot run on this host ({exc.strerror or exc}); "
                         "the update was not installed")
    verdict = _last_json(done.stdout)
    if done.returncode == 0 and verdict.get("ok"):
        return str(verdict.get("version") or "")
    if done.returncode == 2 and "preflight" in (done.stderr or "") and not verdict:
        # argparse refusing the word: a build older than the self-check.
        logger.warning("the new agent has no self-check (it predates 2026.17); "
                       "installing it unchecked")
        return ""
    reason = (verdict.get("error") or _one_line(done.stderr or done.stdout)
              or f"it exited with status {done.returncode}").rstrip(". ")
    raise ValueError(f"the new agent failed its self-check on this host: {reason}. "
                     "The update was not installed")


def _last_json(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:MAX_REASON]


def previous_path(exe: Path) -> Path:
    return exe.with_name(exe.name + ".previous")


def keep_previous(exe: Path) -> Path | None:
    """Keep the running binary beside itself before it is replaced. None when
    it could not be kept: the update goes on, without a way back."""
    target = previous_path(exe)
    try:
        if target.exists():
            target.unlink()
        try:
            os.link(exe, target)  # instant, and the replace leaves it intact
        except OSError:
            shutil.copy2(exe, target)
        return target
    except OSError as exc:
        logger.warning("could not keep the current agent for a rollback: %s", exc)
        return None


def _marker(data_dir: Path) -> Path:
    return Path(data_dir) / MARKER_NAME


def load(data_dir: Path) -> dict | None:
    try:
        data = json.loads(_marker(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def _save(data_dir: Path, data: dict) -> None:
    target = _marker(data_dir)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, target)


def begin(data_dir: Path, *, to_version: str, exe: Path, previous: Path | None) -> None:
    """Record that an update is about to be swapped in."""
    _save(data_dir, {"state": PENDING, "from_version": __version__, "to_version": to_version,
                     "exe": str(exe), "previous": str(previous) if previous else "",
                     "at": int(time.time())})


def forget(data_dir: Path) -> None:
    try:
        _marker(data_dir).unlink()
    except OSError:
        pass


class Probation:
    """The check-in loop's view of an update in flight. Every method is safe
    to call when there is none."""

    def __init__(self, data_dir: Path, *, restart, clock=time.monotonic,
                 version: str = __version__):
        self.data_dir = Path(data_dir)
        self._version = version
        self._restart = restart
        self._clock = clock
        self._started = clock()
        self._failures = 0
        self._marker = load(self.data_dir)
        if self._marker:
            logger.info("agent update %s: %s -> %s", self._marker["state"],
                        self._marker.get("from_version"), self._marker.get("to_version"))

    def report(self) -> dict | None:
        """What the next check-in tells the server, once the outcome is known."""
        marker = self._marker
        if not marker or marker["state"] == PENDING:
            return None
        return {"state": marker["state"],
                "from_version": str(marker.get("from_version", ""))[:50],
                "to_version": str(marker.get("to_version", ""))[:50],
                "running": self._version,
                "reason": str(marker.get("reason", ""))[:MAX_REASON]}

    def checkin_succeeded(self, reported: bool) -> None:
        """*reported* says whether this check-in carried ``report()``."""
        self._failures = 0
        marker = self._marker
        if not marker:
            return
        if marker["state"] == PENDING:
            self._remove_previous(marker)
            target = str(marker.get("to_version") or "unknown")
            if (target not in ("unknown", self._version)
                    and marker.get("from_version") == self._version):
                marker.update(state=NOT_APPLIED, previous="",
                              reason="the agent restarted as the version it already was")
                logger.error("agent update to %s did not take effect", target)
            else:
                # The new agent reached the server: the update is good.
                marker.update(state=CONFIRMED, previous="")
                logger.info("agent update confirmed: now running %s", self._version)
            _save(self.data_dir, marker)
        elif reported:
            forget(self.data_dir)
            self._marker = None

    def checkin_failed(self, error: BaseException | str) -> bool:
        """Count a failed check-in. True when it rolled the update back and a
        restart into the old agent is on its way."""
        self._failures += 1
        marker = self._marker
        if (not marker or marker["state"] != PENDING
                or self._failures < ROLLBACK_AFTER_FAILURES
                or self._clock() - self._started < ROLLBACK_AFTER_SECONDS):
            return False
        previous = Path(marker.get("previous") or "")
        exe = Path(marker.get("exe") or "")
        if not marker.get("previous") or not previous.is_file() or not marker.get("exe"):
            return False  # nothing to go back to (Windows, or it could not be kept)
        reason = _one_line(f"{self._failures} check-ins failed after the update: {error}")
        try:
            os.replace(previous, exe)
        except OSError as exc:
            logger.error("could not put the previous agent back: %s", exc)
            return False
        marker.update(state=ROLLED_BACK, reason=reason, previous="")
        _save(self.data_dir, marker)
        logger.error("agent update rolled back to %s: %s", marker.get("from_version"), reason)
        self._restart()
        return True

    @staticmethod
    def _remove_previous(marker: dict) -> None:
        if marker.get("previous"):
            try:
                Path(marker["previous"]).unlink()
            except OSError:
                pass


def restart_service() -> None:
    """Ask the service manager to restart the agent. Never raises: by the time
    this runs the binary on disk is already the one that should be running."""
    try:
        if sys.platform == "win32":
            subprocess.run(["sc", "stop", "vigil-agent"], timeout=10,
                           capture_output=True, env=clean_env())
            time.sleep(2)
            subprocess.run(["sc", "start", "vigil-agent"], timeout=10,
                           capture_output=True, env=clean_env())
        elif sys.platform == "darwin":
            subprocess.run(["launchctl", "stop", "com.susquehannasyntax.vigil-agent"],
                           timeout=10, capture_output=True, env=clean_env())
            time.sleep(1)
            subprocess.run(["launchctl", "start", "com.susquehannasyntax.vigil-agent"],
                           timeout=10, capture_output=True, env=clean_env())
        elif sys.platform.startswith("freebsd"):
            # The rc.d script install.sh writes runs the agent under
            # `daemon -r`, so the restart lands on the new binary.
            subprocess.run(["service", "vigil_agent", "restart"],
                           timeout=10, capture_output=True, env=clean_env())
        else:
            subprocess.run(["systemctl", "restart", "vigil-agent"],
                           timeout=10, capture_output=True, env=clean_env())
    except Exception:  # noqa: S110 — the swap already landed; nothing left to do
        pass
