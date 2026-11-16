"""Package actions: install / remove / upgrade one package, full updates, temp cleanup.

Moved out of executor.py (which was 1,664 lines). The primary-manager calls go
through the detected ``PackageManager``, reached as ``ex.detect_pkg_manager``
because tests replace that name on the executor module; ``run_package_updates``
shells out for the security-only apt and dnf upgrades through ``ex._run`` for
the same reason. Every operation that can rewrite an initramfs is followed by
``ex._assert_initramfs_clean``, which turns a poisoned image into a failure
before anyone reboots onto it. executor re-exports every name defined here, so
a patch on any of those names reaches what these handlers call.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput

logger = logging.getLogger("vigil.executor")


def _assert_initramfs_clean(output: str) -> str:
    from ..pkg_manager import poisoned_initramfs

    poisoned = poisoned_initramfs()
    if not poisoned:
        return output
    raise RuntimeError(
        "Package operation completed but these initramfs images contain "
        "ephemeral PyInstaller paths and will NOT boot: "
        f"{', '.join(poisoned)}. Rebuild each from an interactive root shell "
        "with `update-initramfs -u -k <version>` before rebooting this host. "
        f"Package output follows:\n{output}"
    )


def _install_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = ex.detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    text = ex._assert_initramfs_clean(pm.install(pkg_name))
    return ActionOutput(text, {"package": pkg_name, "manager": pm.name,
                               "installed_version": pm.installed_version(pkg_name)})


def _remove_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = ex.detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    return ActionOutput(pm.remove(pkg_name), {"package": pkg_name, "manager": pm.name})


def _update_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = ex.detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    text = ex._assert_initramfs_clean(pm.install(pkg_name))  # install upgrades if already present
    return ActionOutput(text, {"package": pkg_name, "manager": pm.name,
                               "installed_version": pm.installed_version(pkg_name)})


def _run_package_updates(params: dict, _config: AgentConfig) -> str:
    security_only = params.get("security_only", False)
    pm = ex.detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")

    pm.refresh()
    outputs = {"manager": pm.name, "security_only": bool(security_only)}

    if security_only:
        # Security-only upgrades only supported for apt and dnf
        if pm.name in ("apt", "apt-get"):
            return ActionOutput(ex._assert_initramfs_clean(ex._run(
                ["apt-get", "upgrade", "-y", "-qq",
                 "-o", "Dir::Etc::SourceList=/etc/apt/sources.list"],
                timeout=600,
            )), outputs)
        if pm.name == "dnf":
            return ActionOutput(ex._assert_initramfs_clean(ex._run(
                ["dnf", "update", "-y", "-q", "--security"], timeout=600
            )), outputs)
        logger.warning(
            "security_only not supported for %s, running full upgrade",
            pm.name,
        )

    return ActionOutput(ex._assert_initramfs_clean(pm.upgrade_all()), outputs)


def _clear_temp_files(params: dict, _config: AgentConfig) -> str:
    """Delete files in the system temp directory older than N days.

    Done in Python rather than by shelling out. The previous implementation ran

        find /tmp -type f -mtime +N -delete

    unconditionally, which on Windows resolved "find" to
    C:\\Windows\\System32\\FIND.exe — a string-search tool that shares only a
    name — and failed with "FIND: Invalid switch". /tmp does not exist there
    either. Doing the walk here means one implementation, no shell, and the
    same semantics everywhere.
    """
    days = int(params.get("older_than_days", 7))
    if days < 0:
        raise ValueError("older_than_days must be non-negative")

    temp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    skipped = 0
    for path in temp_root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            st = path.stat()
            if st.st_mtime >= cutoff:
                continue
            size = st.st_size
            path.unlink()
        except OSError:
            # A temp directory always has files something else holds open,
            # and on Windows that is the normal case rather than the
            # exception. One locked file must not fail the whole task.
            skipped += 1
            continue
        removed += 1
        freed += size

    return ActionOutput(
        (f"Removed {removed} file(s) older than {days} day(s) from "
         f"{temp_root}, freeing {freed // 1024} KiB"
         + (f"; {skipped} in use or not permitted" if skipped else "")),
        {"removed": removed, "skipped": skipped})
