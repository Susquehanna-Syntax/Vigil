"""Persistent nonce tracking for replay protection.

Stores each seen nonce with the time it may be forgotten: no earlier than the
task's own TTL has run out (SEC-4). A fixed one-hour memory let a task whose
TTL was longer be replayed once its nonce had been pruned.
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger("vigil.nonce")

# A new file: entries are expiry times now, where "seen_nonces" held the time a
# nonce was recorded. Reading one as the other would forget nonces too early.
_NONCE_FILENAME = "seen_nonces.v2"
# Never forget a nonce sooner than this, whatever the task's TTL.
_MIN_KEEP_SECONDS = 3600
# Margin past a task's TTL, for clock skew between server and agent.
_SKEW_SECONDS = 600


class NonceStore:
    def __init__(self, data_dir: Path):
        self._path = data_dir / _NONCE_FILENAME
        self._entries: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            for line in self._path.read_text().splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    self._entries[parts[0]] = float(parts[1])
        except (OSError, ValueError):
            logger.warning("Failed to load nonce store, starting fresh")
            self._entries = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{nonce}\t{ts}" for nonce, ts in self._entries.items()]
        self._path.write_text("\n".join(lines) + "\n" if lines else "")
        self._path.chmod(0o600)

    def seen(self, nonce: str) -> bool:
        """Return True if this nonce was already used (replay attempt)."""
        return nonce in self._entries

    def record(self, nonce: str, ttl_seconds: int | float = 0) -> None:
        """Mark a nonce as used, remembered until its task could no longer run."""
        try:
            ttl = max(0.0, float(ttl_seconds))
        except (TypeError, ValueError):
            ttl = 0.0
        keep = max(_MIN_KEEP_SECONDS, ttl + _SKEW_SECONDS)
        self._entries[nonce] = time.time() + keep
        self._prune()
        self._save()

    def _prune(self) -> None:
        """Forget nonces whose keep-until time has passed."""
        now = time.time()
        self._entries = {n: until for n, until in self._entries.items() if until > now}
