"""Task results the server did not take, kept until it does.

A result is posted once, straight after the task runs. When that post fails —
the dashboard restarting behind a relay (503 "Vigil is unreachable"), a tunnel
answering 530, a read timeout — the result used to be logged and dropped, and
the task sat DISPATCHED on the server until the expiry sweep guessed it dead.
An update that failed for a passing reason looked like one that never arrived.

Now a result that could not be delivered for a reason that may pass is written
to ``data_dir/pending_results.json`` and posted again after every check-in that
reaches the server, oldest first, until the server answers. An answer that
says no (4xx: the task is unknown, or already settled) drops it: sending the
same thing again will not change that. Bounded in count and age, so a server
gone for good cannot grow the file without end.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger("vigil.outbox")

STATE_FILE = "pending_results.json"
MAX_PENDING = 200
MAX_AGE_SECONDS = 7 * 24 * 3600

_lock = threading.Lock()
_dir: Path | None = None


def configure(data_dir) -> None:
    """Where the outbox lives. Until called, failed results are only logged."""
    global _dir
    _dir = Path(data_dir)


def worth_retrying(exc: BaseException) -> bool:
    """Whether a failed post may succeed later: no answer, a timeout, or a
    server-side or rate-limit status. A 4xx other than 408/429 is final."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        return code >= 500 or code in (408, 429)
    return False


def _path() -> Path | None:
    return (_dir / STATE_FILE) if _dir is not None else None


def _read() -> list[dict]:
    path = _path()
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("pending results file unreadable; starting it again")
        return []
    return [p for p in data if isinstance(p, dict) and p.get("task_id")] if isinstance(data, list) else []


def _write(items: list[dict]) -> None:
    path = _path()
    if path is None:
        return
    # mkstemp creates the file 0600 with a fresh name (O_EXCL, no symlink
    # followed): task output is never readable by others, not even briefly,
    # in a data_dir that is itself 0755.
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-results-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(items))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        logger.warning("could not save pending results: %s", exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def keep(task_id: str, state: str, output: str, steps=None, now=time.time) -> bool:
    """Hold a result for later. True when it was kept."""
    if _path() is None:
        return False
    with _lock:
        items = [p for p in _read() if p["task_id"] != task_id]
        items.append({"task_id": task_id, "state": state, "output": output,
                      **({"steps": steps} if steps is not None else {}), "kept_at": now()})
        _write(items[-MAX_PENDING:])
    logger.warning("Holding the result of task %s (%s) to send again", task_id, state)
    return True


def pending() -> int:
    with _lock:
        return len(_read())


def flush(send, now=time.time) -> int:
    """Post every held result through ``send(task_id, state, output, steps)``.
    Stops at the first one that may pass (the server is still not answering)
    and keeps the rest. Returns how many were delivered."""
    with _lock:
        items = _read()
        if not items:
            return 0
        cutoff = now() - MAX_AGE_SECONDS
        delivered, left = 0, []
        for i, item in enumerate(items):
            if item.get("kept_at", 0) < cutoff:
                logger.warning("Dropping the held result of task %s: older than a week", item["task_id"])
                continue
            try:
                send(item["task_id"], item["state"], item.get("output", ""), item.get("steps"))
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — sorted by worth_retrying below
                if worth_retrying(exc):
                    left = items[i:]
                    break
                logger.warning("The server refused the held result of task %s: %s", item["task_id"], exc)
        _write([p for p in left if p.get("kept_at", 0) >= cutoff])
    if delivered:
        logger.info("Delivered %d held task result(s)", delivered)
    return delivered
