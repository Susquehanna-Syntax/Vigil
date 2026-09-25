"""The hunt framework: one result shape and one set of guardrails for every hunt.

Contract shared by all hunts (phases 02–05): every hunt returns JSON text with
exactly the keys ``matches`` (list of match dicts), ``truncated`` (bool) and
``duration`` (float seconds), and the handler exposes the declared outputs
``matched`` (bool), ``count`` (int) and ``truncated`` (bool). Hunts are
read-only, bounded (max_results + timeout) and run at low CPU/I/O priority.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

logger = logging.getLogger("vigil.hunt")

DEFAULT_MAX_RESULTS = 500
MAX_RESULTS_CEILING = 5000
DEFAULT_TIMEOUT = 120      # seconds
TIMEOUT_CEILING = 600

_JSON_SAFE = (str, int, float, bool, type(None))


class HuntTimeout(Exception):
    """Raised by HuntResult.check_deadline() once the hunt's deadline passes."""


class HuntResult:
    """Accumulates hunt matches, enforces the result cap and the deadline."""

    def __init__(self, max_results: int, timeout: float):
        self.max_results = max_results
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.matches: list[dict] = []
        self.truncated = False
        self.started = time.monotonic()

    def add(self, evidence_type: str, **fields) -> bool:
        """Record a match. Returns False (and truncates) once max_results is reached."""
        if len(self.matches) >= self.max_results:
            self.truncated = True
            return False
        match = {"evidence_type": evidence_type}
        for key, value in fields.items():
            match[key] = value if isinstance(value, _JSON_SAFE) else str(value)
        self.matches.append(match)
        return True

    def check_deadline(self) -> None:
        """Raise HuntTimeout past the deadline; probes call this inside their loops."""
        if time.monotonic() > self.deadline:
            raise HuntTimeout()

    def to_dict(self) -> dict:
        return {
            "matches": self.matches,
            "truncated": self.truncated,
            "duration": round(time.monotonic() - self.started, 3),
        }


def limits_from(params: dict) -> tuple[int, int]:
    """(max_results, timeout) from params, clamped to the ceilings; ValueError if < 1."""
    try:
        max_results = int(params.get("max_results", DEFAULT_MAX_RESULTS))
        timeout = int(params.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_results and timeout must be integers") from exc
    if max_results < 1 or timeout < 1:
        raise ValueError("max_results and timeout must be >= 1")
    return min(max_results, MAX_RESULTS_CEILING), min(timeout, TIMEOUT_CEILING)


def run_hunt(probe, params: dict):
    """Run a probe in a low-priority worker thread and shape its result."""
    from .executor import ActionOutput

    max_results, timeout = limits_from(params)
    result = HuntResult(max_results, timeout)
    error_box: dict = {}

    def worker():
        _lower_thread_priority()
        try:
            probe(result, params)
        except HuntTimeout:
            result.truncated = True
        except BaseException as exc:  # noqa: BLE001 — surfaced as RuntimeError below
            error_box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout + 5)

    if thread.is_alive():
        result.truncated = True

    probe_error = error_box.get("error")
    if probe_error is not None and probe_error.__class__ is not HuntTimeout:
        raise RuntimeError(f"hunt failed: {probe_error}") from probe_error

    result_dict = result.to_dict()
    if result_dict["truncated"]:
        result_dict["timed_out"] = True

    count = len(result.matches)
    return ActionOutput(
        json.dumps(result_dict, sort_keys=True),
        {"matched": count > 0, "count": count, "truncated": result.truncated},
    )


def default_scopes() -> list[str]:
    """Paths a default hunt scans: only the ones that exist on this host."""
    if sys.platform == "win32":
        scopes = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramData", r"C:\ProgramData"),
        ]
        users_dir = r"C:\Users"
        if os.path.isdir(users_dir):
            for user in sorted(os.listdir(users_dir)):
                base = os.path.join(users_dir, user)
                scopes.append(os.path.join(base, "AppData"))
                scopes.append(os.path.join(base, "Downloads"))
    else:
        scopes = ["/opt", "/usr/local", "/home", "/root", "/srv"]
    return [scope for scope in scopes if os.path.isdir(scope)]


def _lower_thread_priority() -> None:
    """Best effort: lower this thread's CPU/I/O priority. Never raises."""
    try:
        if sys.platform.startswith("linux"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            import psutil

            psutil.Process(threading.get_native_id()).ionice(psutil.IOPRIO_CLASS_IDLE)
        elif sys.platform == "win32":
            import ctypes

            THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
            kernel32 = ctypes.windll.kernel32
            current = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(current, THREAD_MODE_BACKGROUND_BEGIN)
        else:
            logger.debug("thread priority lowering not supported on %s", sys.platform)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("thread priority lowering skipped: %s", exc)
