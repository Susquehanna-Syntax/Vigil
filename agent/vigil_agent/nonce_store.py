"""Persistent nonce tracking for replay protection.

Stores seen nonces in a flat file. Periodically prunes entries older than
the maximum TTL to prevent unbounded growth.
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger("vigil.nonce")

_NONCE_FILENAME = "seen_nonces"
# Keep nonces for 1 hour — well beyond any reasonable task TTL (default 300s)
_MAX_AGE_SECONDS = 3600


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

    def record(self, nonce: str) -> None:
        """Mark a nonce as used."""
        self._entries[nonce] = time.time()
        self._prune()
        self._save()

    def _prune(self) -> None:
        """Remove nonces older than _MAX_AGE_SECONDS."""
        cutoff = time.time() - _MAX_AGE_SECONDS
        self._entries = {n: ts for n, ts in self._entries.items() if ts > cutoff}
