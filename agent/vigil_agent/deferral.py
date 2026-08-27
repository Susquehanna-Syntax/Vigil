"""Persistent reboot-deferral state, enforced agent-side.

When a reboot task is dispatched with ``defer_limit`` / ``defer_minutes``,
the person in front of the machine may postpone it up to ``defer_limit``
times, each for up to ``defer_minutes``. The state lives in a small JSON file
beside the agent's other state (location and 0o600 permissions follow
``nonce_store.py``).

Enforcement is deliberately agent-side: the agent's own state is
authoritative, and a laptop that is offline when its deferral expires still
reboots on schedule once it is back. The budget resets when the reboot
happens (``clear``) or when a different task id arrives (``record``).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("vigil.deferral")

_FILENAME = "reboot_deferral.json"


class RebootDeferral:
    def __init__(self, data_dir: Path):
        self._path = data_dir / _FILENAME
        self._task_id = ""
        self._limit = 0
        self._defer_minutes = 0
        self._used = 0
        self._expires_at = 0.0  # monotonic deadline; 0.0 = no pending window
        self._load()

    # ── Load / persist ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._task_id = str(data.get("task_id", ""))
            self._limit = int(data.get("limit", 0))
            self._defer_minutes = int(data.get("defer_minutes", 0))
            self._used = int(data.get("used", 0))
            # Wall-clock timestamp, so the countdown survives an agent
            # restart mid-window; re-anchored onto monotonic on load.
            self._expires_at = max(0.0, float(data.get("expires_at", 0.0)) - time.time())
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to load reboot deferral state, starting fresh")
            self._task_id, self._limit, self._defer_minutes = "", 0, 0
            self._used, self._expires_at = 0, 0.0

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "task_id": self._task_id,
            "limit": self._limit,
            "defer_minutes": self._defer_minutes,
            "used": self._used,
            "expires_at": time.time() + self._expires_at if self._expires_at else 0.0,
        }
        self._path.write_text(json.dumps(data))
        self._path.chmod(0o600)

    # ── API ─────────────────────────────────────────────────────────────

    def record(self, task_id: str, defer_limit: int, defer_minutes: int) -> None:
        """Register a deferrable reboot task. A different task id resets
        the budget; the same id keeps its deferrals used and expiry."""
        if task_id != self._task_id:
            self._task_id = task_id
            self._limit = defer_limit
            self._defer_minutes = defer_minutes
            self._used = 0
            self._expires_at = 0.0
        self._save()

    def defer(self, minutes: int) -> None:
        """Consume one deferral and start the expiry countdown."""
        self._used += 1
        self._expires_at = minutes * 60.0
        self._save()

    def expiry_remaining(self) -> float:
        """Seconds until the pending deferral expires; 0.0 when none."""
        return max(0.0, self._expires_at)

    def expired(self) -> bool:
        """True once a pending deferral's window has elapsed."""
        return self.expiry_remaining() == 0.0

    def exhausted(self) -> bool:
        """True when no deferrals remain and the current window has expired."""
        return self._used >= self._limit and self._limit > 0 and self.expired()

    def clear(self) -> None:
        """The reboot happened (or was cancelled): drop all deferral state."""
        self._task_id, self._limit, self._defer_minutes = "", 0, 0
        self._used, self._expires_at = 0, 0.0
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            logger.warning("Failed to remove reboot deferral state file")
