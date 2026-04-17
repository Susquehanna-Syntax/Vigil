"""Cross-platform package manager detection and dispatch.

Detects the available package manager on the current system and provides
a unified interface for refresh / upgrade / install / remove / list_upgradable.

Detection order (first found wins):
  Linux:    apt-get → dnf → yum → pacman → zypper → apk → snap
  macOS:    brew
  Windows:  winget → choco → scoop

All commands use subprocess with explicit argument lists (never shell=True).
"""

import logging
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("vigil.pkg_manager")

_EXEC_TIMEOUT_SHORT = 30
_EXEC_TIMEOUT_LONG = 600


def _which(name: str) -> bool:
    """Return True if *name* is on PATH."""
    try:
        result = subprocess.run(
            ["which", name] if sys.platform != "win32" else ["where", name],
            capture_output=True,
            timeout=5,
            shell=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run(cmd: list[str], timeout: int = _EXEC_TIMEOUT_LONG) -> str:
    logger.info("pkg_manager: %s", cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode not in (0, 100):  # apt returns 100 when upgrades available
        raise RuntimeError(f"Command exited {result.returncode}: {output}")
    return output


@dataclass
class PackageManager:
    name: str

    # ── Public interface ──────────────────────────────────────────────────

    def refresh(self) -> str:
        return self._refresh()

    def upgrade_all(self) -> str:
        return self._upgrade_all()

    def install(self, package_name: str) -> str:
        _validate_package_name(package_name)
        return self._install(package_name)

    def remove(self, package_name: str) -> str:
        _validate_package_name(package_name)
        return self._remove(package_name)

    def list_upgradable(self) -> str:
        return self._list_upgradable()

    # ── apt-get / apt ─────────────────────────────────────────────────────

    def _refresh(self) -> str:
        if self.name in ("apt", "apt-get"):
            return _run(["apt-get", "update", "-qq"])
        if self.name == "dnf":
            return _run(["dnf", "check-update", "--quiet"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "yum":
            return _run(["yum", "check-update", "-q"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "pacman":
            return _run(["pacman", "-Sy", "--noconfirm"])
        if self.name == "zypper":
            return _run(["zypper", "refresh", "-q"])
        if self.name == "apk":
            return _run(["apk", "update", "-q"])
        if self.name == "brew":
            return _run(["brew", "update", "--quiet"])
        if self.name == "winget":
            return _run(["winget", "source", "update", "--disable-interactivity"])
        if self.name == "snap":
            return _run(["snap", "refresh", "--list"])
        raise RuntimeError(f"refresh not implemented for {self.name}")

    def _upgrade_all(self) -> str:
        if self.name in ("apt", "apt-get"):
            return _run(["apt-get", "upgrade", "-y", "-qq"])
        if self.name == "dnf":
            return _run(["dnf", "upgrade", "-y", "--quiet"])
        if self.name == "yum":
            return _run(["yum", "update", "-y", "-q"])
        if self.name == "pacman":
            return _run(["pacman", "-Syu", "--noconfirm"])
        if self.name == "zypper":
            return _run(["zypper", "update", "-y", "-q"])
        if self.name == "apk":
            return _run(["apk", "upgrade", "-q"])
        if self.name == "brew":
            return _run(["brew", "upgrade", "--quiet"])
        if self.name == "winget":
            return _run(["winget", "upgrade", "--all", "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"])
        if self.name == "snap":
            return _run(["snap", "refresh"])
        raise RuntimeError(f"upgrade_all not implemented for {self.name}")

    def _install(self, pkg: str) -> str:
        if self.name in ("apt", "apt-get"):
            return _run(["apt-get", "install", "-y", "-qq", pkg])
        if self.name == "dnf":
            return _run(["dnf", "install", "-y", "--quiet", pkg])
        if self.name == "yum":
            return _run(["yum", "install", "-y", "-q", pkg])
        if self.name == "pacman":
            return _run(["pacman", "-S", "--noconfirm", pkg])
        if self.name == "zypper":
            return _run(["zypper", "install", "-y", "-q", pkg])
        if self.name == "apk":
            return _run(["apk", "add", "-q", pkg])
        if self.name == "brew":
            return _run(["brew", "install", "--quiet", pkg])
        if self.name == "winget":
            return _run(["winget", "install", pkg, "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"])
        if self.name == "snap":
            return _run(["snap", "install", pkg])
        raise RuntimeError(f"install not implemented for {self.name}")

    def _remove(self, pkg: str) -> str:
        if self.name in ("apt", "apt-get"):
            return _run(["apt-get", "remove", "-y", "-qq", pkg])
        if self.name == "dnf":
            return _run(["dnf", "remove", "-y", "--quiet", pkg])
        if self.name == "yum":
            return _run(["yum", "remove", "-y", "-q", pkg])
        if self.name == "pacman":
            return _run(["pacman", "-R", "--noconfirm", pkg])
        if self.name == "zypper":
            return _run(["zypper", "remove", "-y", "-q", pkg])
        if self.name == "apk":
            return _run(["apk", "del", "-q", pkg])
        if self.name == "brew":
            return _run(["brew", "uninstall", "--quiet", pkg])
        if self.name == "winget":
            return _run(["winget", "uninstall", pkg, "--disable-interactivity"])
        if self.name == "snap":
            return _run(["snap", "remove", pkg])
        raise RuntimeError(f"remove not implemented for {self.name}")

    def _list_upgradable(self) -> str:
        if self.name in ("apt", "apt-get"):
            return _run(["apt", "list", "--upgradable"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "dnf":
            return _run(["dnf", "list", "updates", "--quiet"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "yum":
            return _run(["yum", "list", "updates", "-q"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "pacman":
            return _run(["pacman", "-Qu"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "zypper":
            return _run(["zypper", "list-updates", "-q"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "apk":
            return _run(["apk", "list", "--upgradable", "-q"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "brew":
            return _run(["brew", "outdated", "--quiet"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "winget":
            return _run(["winget", "upgrade", "--disable-interactivity"], timeout=_EXEC_TIMEOUT_SHORT)
        if self.name == "snap":
            return _run(["snap", "refresh", "--list"], timeout=_EXEC_TIMEOUT_SHORT)
        raise RuntimeError(f"list_upgradable not implemented for {self.name}")


def detect() -> Optional[PackageManager]:
    """Detect the package manager available on this system.

    Returns None if no supported package manager is found.
    """
    system = platform.system()

    if system == "Darwin":
        candidates = ["brew"]
    elif system == "Windows":
        candidates = ["winget", "choco", "scoop"]
    else:
        # Linux and other Unix-likes
        candidates = ["apt-get", "dnf", "yum", "pacman", "zypper", "apk", "snap"]

    for name in candidates:
        if _which(name):
            logger.debug("Detected package manager: %s", name)
            return PackageManager(name=name)

    logger.warning("No supported package manager found on this system")
    return None


_SAFE_PKG_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.+:@/"
)


def _validate_package_name(name: str) -> None:
    """Reject package names with shell metacharacters."""
    if not name or len(name) > 256:
        raise ValueError(f"Invalid package name: {name!r}")
    invalid = set(name) - _SAFE_PKG_NAME_CHARS
    if invalid:
        raise ValueError(f"Package name contains invalid characters {invalid!r}: {name!r}")
