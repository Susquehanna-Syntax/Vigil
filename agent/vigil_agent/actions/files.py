"""File and directory actions: write, mkdir, delete, copy, move, chmod/chown.

Moved out of executor.py (which was 1,664 lines). Every path is resolved and
validated before it touches the filesystem: ``_validate_path`` refuses the
pseudo-filesystems, ``_validate_write_path`` additionally refuses the sensitive
files and the agent's own data directory (so a copy or a move destination gets
the same protection as a write), and ``_validate_delete_path`` refuses the
paths whose loss makes a host unbootable. None of these handlers shell out, so
only the plain executor helpers are imported here; executor re-exports every
name defined in this module.
"""

from __future__ import annotations

import os
import shutil

from ..config import AgentConfig
from ..executor import (
    ActionOutput,
    _chown,
    _parse_octal_mode,
    _validate_delete_path,
    _validate_path,
    _validate_write_path,
)


def _write_file(params: dict, config: AgentConfig) -> str:
    path = _validate_write_path(params.get("path", ""), config)
    content = params.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    return ActionOutput(f"Wrote {len(content)} bytes to {path}",
                        {"path": str(path), "bytes": len(content)})


def _create_directory(params: dict, _config: AgentConfig) -> str:
    path = _validate_path(params.get("path", ""), "path")
    path.mkdir(parents=True, exist_ok=True)

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    owner = str(params.get("owner", ""))
    group = str(params.get("group", ""))
    if owner or group:
        _chown(path, owner, group)

    return ActionOutput(f"Created directory {path}", {"path": str(path)})


def _delete_path(params: dict, _config: AgentConfig) -> str:
    recursive = bool(params.get("recursive", False))
    path = _validate_delete_path(params.get("path", ""), recursive)

    if path.is_dir():
        shutil.rmtree(path)
        return ActionOutput(f"Deleted directory {path} (recursive)",
                            {"path": str(path), "recursive": recursive})
    else:
        path.unlink()
        return ActionOutput(f"Deleted {path}",
                            {"path": str(path), "recursive": recursive})


def _copy_file(params: dict, config: AgentConfig) -> str:
    src = _validate_path(params.get("src", ""), "src")
    # The destination is written to, so it gets the sensitive-file checks —
    # _validate_path alone would let a copy land on /etc/shadow.
    dest = _validate_write_path(params.get("dest", ""), config)
    if not src.exists():
        raise ValueError(f"Source not found: {src}")

    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return ActionOutput(f"Copied {src} -> {dest}", {"src": str(src), "dest": str(dest)})


def _move_file(params: dict, config: AgentConfig) -> str:
    # A move both writes the destination and removes the source, so both
    # ends go through the sensitive-path checks.
    src = _validate_write_path(params.get("src", ""), config)
    dest = _validate_write_path(params.get("dest", ""), config)
    if not src.exists():
        raise ValueError(f"Source not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return ActionOutput(f"Moved {src} -> {dest}", {"src": str(src), "dest": str(dest)})


def _set_permissions(params: dict, _config: AgentConfig) -> str:
    path = _validate_path(params.get("path", ""), "path")
    if not path.exists():
        raise ValueError(f"Path not found: {path}")

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    owner = str(params.get("owner", ""))
    group = str(params.get("group", ""))
    if owner or group:
        _chown(path, owner, group)

    parts = []
    if mode:
        parts.append(f"mode={mode}")
    if owner:
        parts.append(f"owner={owner}")
    if group:
        parts.append(f"group={group}")
    return ActionOutput(f"Set {', '.join(parts)} on {path}", {"path": str(path)})
