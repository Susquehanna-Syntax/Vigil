"""Session recording: asciicast v2, Business only.

The shell's output is the evidence a security review asks for after the fact,
so it is written in a format an existing player can replay (asciicast v2, as
terminal-recorder-tools and asciinema use) rather than a Vigil-private one.

Only when ``has_feature("audit_log")`` — recording the whole session is the
paid tier's; taking an audit trail of who opened what is every tier's.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from django.conf import settings

from vigil.licensing import has_feature

RECORDING_DIR = "recordings"


def recording_allowed(kind: str) -> bool:
    """Shell and container output is text we can record here. RDP is not — its
    stream belongs to guacd, which records its own (QA-59)."""
    if kind in ("shell", "container"):
        return has_feature("audit_log")
    return False


class CastWriter:
    """One ``.cast`` file: a header line, then one line per output chunk.

    Writes go through ``asyncio.to_thread`` — a slow disk must not stall the
    event loop that is pumping both sockets.
    """

    def __init__(self, path: Path, relative_path: str, cols: int = 120, rows: int = 32):
        self._path = path
        self.relative_path = relative_path
        self._started = time.time()
        # Owner-only from the first byte: a session's output can hold anything.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8")
        self._write({
            "version": 2,
            "width": cols,
            "height": rows,
            "timestamp": int(self._started),
        })

    @classmethod
    def for_session(cls, session, cols: int = 120, rows: int = 32):
        root = Path(settings.MEDIA_ROOT) / RECORDING_DIR
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        # MEDIA_ROOT is served as /media/ when DEBUG is on, and session ids are
        # listed in the API. An unguessable name keeps the recording reachable
        # only through the admin-and-Business download view.
        name = f"{session.id}-{secrets.token_hex(16)}.cast"
        return cls(root / name, f"{RECORDING_DIR}/{name}", cols=cols, rows=rows)

    async def write_output(self, data: bytes) -> None:
        await asyncio.to_thread(self._line, [self._elapsed(), "o",
                                             data.decode("utf-8", errors="replace")])

    async def resize(self, cols, rows) -> None:
        if not (isinstance(cols, int) and isinstance(rows, int)):
            return
        await asyncio.to_thread(self._line, [self._elapsed(), "r", f"{cols}x{rows}"])

    async def aclose(self) -> None:
        await asyncio.to_thread(self._file.close)

    def _elapsed(self) -> float:
        return round(time.time() - self._started, 6)

    def _line(self, row: list) -> None:
        if self._file.closed:
            return
        self._write(row)

    def _write(self, payload) -> None:
        # Output text is attacker-controlled: an invalid surrogate must not
        # take the socket down on the way to disk.
        self._file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._file.flush()
