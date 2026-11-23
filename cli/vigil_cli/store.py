"""Where a sign-in is kept between commands: one JSON file, readable only by
its owner. It holds the server address and the session cookies, never the
password and never an API key."""
from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    override = os.environ.get("VIGIL_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vigil" / "session.json"


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    target = path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Created 0600 from the start: write-then-chmod would leave a moment where
    # another user on the machine could read a live session.
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
        os.chmod(tmp, 0o600)  # an older file's looser mode does not survive
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass
