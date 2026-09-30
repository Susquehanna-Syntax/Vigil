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
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .procenv import clean_env

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
            env=clean_env(),
        )
        return result.returncode == 0
    except Exception:
        return False


def _run(cmd: list[str], timeout: int = _EXEC_TIMEOUT_LONG,
         env: dict[str, str] | None = None) -> str:
    logger.info("pkg_manager: %s", cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        env=env or clean_env(),
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode not in (0, 100):  # apt returns 100 when upgrades available
        raise RuntimeError(f"Command exited {result.returncode}: {output}")
    return output


@dataclass
class PackageManager:
    name: str
    #: Absolute path to the binary, when it is not resolvable from PATH.
    #: winget on Windows is a per-user App Execution Alias living under
    #: %LOCALAPPDATA%, so a service running as LocalSystem — which has no user
    #: profile — cannot find it by name and reported "No supported package
    #: manager found" on a machine that plainly had one.
    path: str = ""

    def _bin(self) -> str:
        return self.path or self.name

    def _env(self) -> dict[str, str] | None:
        """Environment for this manager's commands.

        winget launched outside its package context cannot find its VCLibs C
        runtime (STATUS_DLL_NOT_FOUND as LocalSystem) — see winget_env().
        """
        if self.name == "winget":
            return winget_env(self._bin())
        return None

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

    def installed_version(self, package_name: str) -> str:
        """Version of *package_name* as installed, or "" when unknown.

        Informational only: an empty result (package not installed, manager not
        queried here, or the query failing) must never fail the step. apk and
        winget are not queried in this phase (Apps/M7 owns them).
        """
        try:
            if self.name in ("apt", "apt-get"):
                return _run(["dpkg-query", "-W", "-f=${Version}", package_name],
                            timeout=_EXEC_TIMEOUT_SHORT).strip()
            if self.name in ("dnf", "yum", "zypper"):
                return _run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}",
                             package_name], timeout=_EXEC_TIMEOUT_SHORT).strip()
            if self.name == "pacman":
                fields = _run(["pacman", "-Q", package_name],
                              timeout=_EXEC_TIMEOUT_SHORT).split()
                return fields[1] if len(fields) > 1 else ""
            if self.name == "brew":
                output = _run(["brew", "list", "--versions", package_name],
                              timeout=_EXEC_TIMEOUT_SHORT).strip()
                return output.rsplit(" ", 1)[-1] if output else ""
            if self.name == "snap":
                lines = _run(["snap", "list", package_name],
                             timeout=_EXEC_TIMEOUT_SHORT).splitlines()
                if len(lines) > 1:
                    return lines[1].split()[1]
                return ""
        except Exception as exc:
            logger.debug("installed_version(%s) via %s inconclusive: %s",
                         package_name, self.name, exc)
        return ""

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
            return _run([self._bin(), "source", "update", "--disable-interactivity"],
                        env=self._env())
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
            return _run([self._bin(), "upgrade", "--all", "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"],
                        env=self._env())
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
            return _run([self._bin(), "install", pkg, "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"], env=self._env())
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
            return _run([self._bin(), "uninstall", pkg, "--disable-interactivity"], env=self._env())
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
            return _run([self._bin(), "upgrade", "--disable-interactivity"], timeout=_EXEC_TIMEOUT_SHORT, env=self._env())
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

    if system == "Windows":
        resolved = _resolve_winget()
        if resolved:
            logger.debug("Detected winget outside PATH at %s", resolved)
            return PackageManager(name="winget", path=resolved)

    logger.warning("No supported package manager found on this system")
    return None


def _windows_apps_root() -> Path:
    """``%ProgramFiles%\\WindowsApps`` — the machine-wide package directory."""
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"


def resolve_winget() -> tuple[str, str]:
    """Locate winget inside the machine-wide App Installer package.

    Returns ``(path, "")`` when found, ``("", "absent")`` when the package is
    not there, and ``("", "denied")`` when the package directory cannot be
    listed. The two failures used to look identical and both were silent; a
    monitor-mode service account is refused ``WindowsApps`` by its ACL, and the
    server then showed a short software list with no hint why.

    Any ``OSError`` other than a denial is treated as "absent": an unusable
    directory yields no package manager, which is what absent means here.
    """
    if sys.platform != "win32":
        return "", "absent"
    root = _windows_apps_root()
    try:
        candidates = sorted(
            root.glob("Microsoft.DesktopAppInstaller_*_x64__*/winget.exe"),
            reverse=True,
        )
    except PermissionError:
        return "", "denied"
    except OSError:
        return "", "absent"
    for candidate in candidates:
        try:
            is_file = candidate.is_file()
        except OSError:
            continue
        if is_file:
            return str(candidate), ""
    return "", "absent"


def _resolve_winget() -> str:
    """Absolute path to winget.exe when it is not on PATH, or "".

    winget ships as the Microsoft.DesktopAppInstaller package and is exposed
    to interactive users through an App Execution Alias in
    %LOCALAPPDATA%\\Microsoft\\WindowsApps. A service running as LocalSystem
    has no such profile, so `where winget` fails and every package action was
    unavailable in the only supported way to run the agent. The package itself
    is machine-wide, so resolve it there instead.
    """
    return resolve_winget()[0]


_VCLIBS_GLOB = "Microsoft.VCLibs.140.00.UWPDesktop_*_x64__8wekyb3d8bbwe"


def winget_env(winget_path: str) -> dict[str, str]:
    """Environment winget needs to start, or the plain sanitized one.

    The ``winget.exe`` under ``%ProgramFiles%\\WindowsApps`` is a packaged
    binary. Launched outside its package context it cannot find its VCLibs C
    runtime and exits -1073741515 (0xC0000135, STATUS_DLL_NOT_FOUND) — which
    is what every Windows agent running as LocalSystem did until this. Putting
    the VCLibs package directory ahead of PATH puts the runtime where the
    loader looks (verified as ``nt authority\\system`` on the VM 2026-09-29:
    plain exit -1073741515, with-VCLibs exit 0).

    Only the machine-wide package directory counts: a per-user App Execution
    Alias under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` is a reparse point
    into the same package and needs no help. Never raises — a VCLibs directory
    that cannot be listed leaves the environment unchanged.
    """
    plain = clean_env()
    if not winget_path:
        return plain
    root = _windows_apps_root()
    # resolve() would follow the per-user App Execution Alias back into the
    # machine-wide package, so containment is decided on the literal path.
    try:
        resolved = Path(winget_path)
        resolved.relative_to(root)
    except ValueError:
        return plain
    try:
        vclibs = sorted(root.glob(_VCLIBS_GLOB), reverse=True)
    except OSError:
        return plain
    for candidate in vclibs:
        try:
            is_dir = candidate.is_dir()
        except OSError:
            continue
        if is_dir:
            return clean_env(extra={
                "PATH": os.pathsep.join(
                    [str(candidate), str(resolved.parent),
                     os.environ.get("PATH", "")]),
            })
    return plain


_SAFE_PKG_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.+:@/"
    # "=" and "~" appear in version pins and Debian versions (openssl=3.0.13-1,
    # 1.2~rc1); they are safe because every command is an argv list, no shell.
    "=~"
)


def validate_app_identifier(value: str, *, version: bool = False) -> bool:
    """True when *value* is safe to pass as an app id (or a version pin).

    Deliberately a different rule from :func:`_validate_package_name`, which
    refuses ``-`` anywhere: an app is addressed by the id the Apps page shows,
    and those ids are full of hyphens (``libgl1``, ``python3-pip``, a Windows
    GUID). What has to be refused is a leading ``-`` (it would read as an
    option), whitespace and shell metacharacters.

    Same two patterns as ``apps.tasks.spec``; kept here as a separate copy
    because the agent never imports server code, and re-checks the wire rather
    than trusting that it was checked.
    """
    pattern = _APP_VERSION_PATTERN if version else _APP_ID_PATTERN
    return pattern.fullmatch(value) is not None


#: ``^[A-Za-z0-9{]…`` — the leading ``{`` is for a Windows registry product code
#: (``{0158093D-…}``); a leading ``-`` is refused so an id can never read as an option.
_APP_ID_PATTERN = re.compile(r"^[A-Za-z0-9{][A-Za-z0-9._+:@/{}~-]{0,199}$")
_APP_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,79}$")


def _validate_package_name(name: str) -> None:
    """Reject package names with shell metacharacters."""
    if not name or len(name) > 256:
        raise ValueError(f"Invalid package name: {name!r}")
    invalid = set(name) - _SAFE_PKG_NAME_CHARS
    if invalid:
        raise ValueError(f"Package name contains invalid characters {invalid!r}: {name!r}")


_INITRAMFS_DIR = "/boot"


def poisoned_initramfs() -> list[str]:
    """Return initramfs images containing PyInstaller extraction paths.

    An image that references /tmp/_MEI… cannot decompress .ko.zst modules at
    early boot, so the host panics before journald starts. Returns [] on any
    platform or toolchain where the check cannot run — an inconclusive probe
    must never be reported as a positive.
    """
    import glob
    import shutil as _shutil

    if sys.platform != "linux" or not _shutil.which("lsinitramfs"):
        return []

    poisoned = []
    for image in sorted(glob.glob(f"{_INITRAMFS_DIR}/initrd.img-*")):
        try:
            result = subprocess.run(
                ["lsinitramfs", image],
                capture_output=True, text=True, timeout=120,
                shell=False, env=clean_env(),
            )
        except Exception as exc:
            logger.warning("Could not read %s (%s); poison check inconclusive",
                           image, exc)
            continue
        if result.returncode != 0:
            logger.warning("lsinitramfs %s exited %d; poison check inconclusive",
                           image, result.returncode)
            continue
        if any(line.startswith("tmp/_MEI")
               for line in result.stdout.splitlines()):
            poisoned.append(image)
    return poisoned
