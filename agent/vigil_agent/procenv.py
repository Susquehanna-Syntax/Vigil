"""Environment sanitizing for child processes.

PyInstaller's --onefile bootloader extracts bundled shared libraries to
/tmp/_MEI<random>/ and points LD_LIBRARY_PATH at it. That is an implementation
detail of the bundle and must never reach a child: `ldd` honours it ahead of
the standard search path, so an agent-driven `update-initramfs` writes the
ephemeral paths into the initramfs image and the host stops booting.

The bootloader preserves any pre-existing value in <VAR>_ORIG. Restore that
when present; otherwise drop the variable and strip any component pointing
into the extraction directory.
"""

from __future__ import annotations

import os
import sys

_LOADER_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_FRAMEWORK_PATH",
)


def bundle_dir() -> str:
    """Return the PyInstaller extraction directory, or "" when not frozen."""
    if not getattr(sys, "frozen", False):
        return ""
    return getattr(sys, "_MEIPASS", "") or ""


def _strip_bundle_paths(value: str, bundle: str) -> str:
    kept = [p for p in value.split(os.pathsep) if p and p != bundle]
    return os.pathsep.join(kept)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of os.environ safe to hand to a child process."""
    env = dict(os.environ)
    bundle = bundle_dir()

    for var in _LOADER_VARS:
        original = env.pop(f"{var}_ORIG", None)
        if original:
            env[var] = original
            continue
        if original is not None:
            env.pop(var, None)
            continue
        if bundle and var in env:
            remaining = _strip_bundle_paths(env[var], bundle)
            if remaining:
                env[var] = remaining
            else:
                env.pop(var, None)

    if extra:
        env.update(extra)
    return env
