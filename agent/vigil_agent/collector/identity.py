r"""Reboot-required probe and the machine fingerprint.

Both are per-host identity/condition probes carried in the check-in payload
rather than time-series metrics, and both share one rule: a *detection* failure
is never reported as a result, and it must never raise into a check-in — so any
probe error means False / "". ``winreg`` is imported lazily inside the Windows
branches because it is stdlib there only.

The suite patches the module object behind these calls (``sys.platform``,
``subprocess.run``) rather than the collector, so this module keeps plain
imports of them; only the helpers another module shares come from ``_common``.
"""

import re
import subprocess
import sys
from pathlib import Path

from ..procenv import clean_env


def reboot_required() -> bool:
    if sys.platform == "win32":
        try:
            return _reboot_required_windows()
        except Exception:
            return False
    if sys.platform == "darwin":
        return False
    try:
        return _reboot_required_linux()
    except Exception:
        return False


def _reboot_required_linux() -> bool:
    if Path("/var/run/reboot-required").exists():
        return True
    try:
        proc = subprocess.run(
            ["dnf", "needs-restarting", "-r"],
            capture_output=True, text=True, timeout=10, shell=False,
            env=clean_env(),
        )
        return proc.returncode != 0
    except Exception:
        return False


def _reboot_required_windows() -> bool:
    # winreg is stdlib on Windows only — import it lazily so the module
    # still imports on Linux.
    import winreg

    keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    )
    for key_path in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path):
                return True
        except OSError:
            continue
    return False


def machine_fingerprint() -> str:
    r"""A stable per-machine id, or "" when the platform will not give one.

    Linux/BSD: systemd's /etc/machine-id (falls back to D-Bus's copy).
    Windows: HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid.
    macOS: IOPlatformUUID from ioreg.
    Never raises: a host that cannot be fingerprinted enrols the old way.
    """
    try:
        if sys.platform == "win32":
            return _machine_fingerprint_windows()
        if sys.platform == "darwin":
            return _machine_fingerprint_macos()
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = Path(path).read_text().strip()
            except OSError:
                continue
            if value:
                return value[:200]
    except Exception:
        pass
    return ""


def _machine_fingerprint_windows() -> str:
    # winreg is stdlib on Windows only — import it lazily so the module
    # still imports on Linux.
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
    return str(value).strip()[:200]


def _machine_fingerprint_macos() -> str:
    proc = subprocess.run(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    match = re.search(r'IOPlatformUUID"\s*=\s*"([^"]+)"', proc.stdout)
    if not match:
        return ""
    return match.group(1).strip()[:200]
