"""Host inventory: hardware, OS, network, disks.

Collected once and shipped alongside the metrics payload on its own (slower)
cadence — hardware moves at human timescales. Every sub-probe is optional: a
host that cannot read ``/proc/cpuinfo`` or reach ``dmidecode`` still gets an
inventory row with the fields it could fill.
"""

import platform
import re
from pathlib import Path

from ..procenv import clean_env
from ._common import psutil, shutil, subprocess
from .disks import _is_real_disk


def collect_inventory() -> dict:
    """Best-effort hardware inventory snapshot.

    Returns a dict with whatever information could be gathered. Fields that
    require root or privileged access (DMI reads can on some distros) fall
    back to "" / 0. The server stores this verbatim alongside the host.
    """
    inv: dict = {}
    try:
        mem = psutil.virtual_memory()
        inv["ram_total_bytes"] = int(mem.total)
    except Exception:
        inv["ram_total_bytes"] = 0

    try:
        inv["cpu_cores"] = int(psutil.cpu_count(logical=True) or 0)
    except Exception:
        inv["cpu_cores"] = 0

    cpu_model = ""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            for line in cpuinfo.read_text(errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    _, _, value = line.partition(":")
                    cpu_model = value.strip()
                    break
        except OSError:
            pass
    if not cpu_model:
        cpu_model = platform.processor() or platform.machine() or ""
    inv["cpu_model"] = cpu_model

    for key, candidates in _DMI_FIELDS.items():
        inv[key] = _read_dmi_field(candidates)

    # If DMI failed entirely, try `dmidecode -s` as a privileged fallback.
    if not any(inv.get(k) for k in _DMI_FIELDS) and shutil.which("dmidecode"):
        for field, dmi_arg in (
            ("service_tag", "system-serial-number"),
            ("manufacturer", "system-manufacturer"),
            ("model", "system-product-name"),
        ):
            try:
                proc = subprocess.run(
                    ["dmidecode", "-s", dmi_arg],
                    capture_output=True, text=True, timeout=5, shell=False,
                    env=clean_env(),
                )
                if proc.returncode == 0:
                    inv[field] = proc.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                continue

    # OS info — try host path first so Flatpak/container agents report the real OS
    _os_release: dict = {}
    for _os_rel_path in [Path("/run/host/os-release"), Path("/etc/os-release")]:
        if _os_rel_path.exists():
            try:
                for _line in _os_rel_path.read_text(errors="replace").splitlines():
                    _k, _, _v = _line.partition("=")
                    _os_release[_k.strip()] = _v.strip().strip('"')
                break
            except OSError:
                continue
    inv["os_name"] = (
        _os_release.get("PRETTY_NAME")
        or _os_release.get("NAME", "")
        or f"{platform.system()} {platform.release()}"
    ).strip()
    inv["os_version"] = (_os_release.get("VERSION_ID") or platform.release()).strip()
    inv["kernel_version"] = platform.release()
    inv["architecture"] = platform.machine()

    # Auto-tag inputs — the server uses these to derive os/pkg/arch
    # tags on Host.tags so target_tags: filters and conditional steps
    # have something to match against. All best-effort; missing fields
    # turn into empty strings and the server skips that tag.
    inv["os_family"] = _detect_os_family(_os_release)
    inv["arch_normalized"] = _normalize_arch(platform.machine())
    try:
        from .pkg_manager import detect as _detect_pkg
        _pm = _detect_pkg()
        inv["pkg_manager"] = _pm.name if _pm else ""
    except Exception:
        inv["pkg_manager"] = ""

    # Uptime
    try:
        import time as _time_mod
        inv["uptime_seconds"] = int(_time_mod.time() - psutil.boot_time())
    except Exception:
        inv["uptime_seconds"] = 0

    # Last logged-in user
    try:
        _users = psutil.users()
        if _users:
            _last_user = max(_users, key=lambda u: u.started)
            inv["last_logged_user"] = _last_user.name
        else:
            inv["last_logged_user"] = ""
    except Exception:
        inv["last_logged_user"] = ""

    # BIOS info from DMI
    inv["bios_version"] = _read_dmi_field(["bios_version"])
    inv["bios_date"] = _read_dmi_field(["bios_date"])

    # System timezone
    try:
        _tz_path = Path("/etc/timezone")
        if _tz_path.exists():
            inv["system_timezone"] = _tz_path.read_text(errors="replace").strip()
        else:
            import time as _time_mod2
            inv["system_timezone"] = (_time_mod2.tzname or ("",))[0]
    except Exception:
        inv["system_timezone"] = ""

    inv["mac_addresses"] = _read_mac_addresses()
    inv["disks"] = _read_disks()

    return inv

_DMI_PATH = Path("/sys/class/dmi/id")
_DMI_FIELDS = {
    "service_tag": ["product_serial", "chassis_serial", "board_serial"],
    "manufacturer": ["sys_vendor", "board_vendor", "chassis_vendor"],
    "model": ["product_name", "board_name"],
}


def _read_dmi_field(filenames: list[str]) -> str:
    """Best-effort read of /sys/class/dmi/id/*. Returns "" on any failure."""
    for name in filenames:
        path = _DMI_PATH / name
        try:
            value = path.read_text(errors="replace").strip()
        except (OSError, PermissionError):
            continue
        if value and value.lower() not in {"to be filled by o.e.m.", "unknown", "default string", "system manufacturer", "system product name"}:
            return value
    return ""


def _read_mac_addresses() -> dict[str, str]:
    """Return {iface: mac} for every non-loopback interface psutil reports."""
    out: dict[str, str] = {}
    try:
        addrs = psutil.net_if_addrs()
    except Exception:
        return out
    for iface, entries in addrs.items():
        if iface == "lo":
            continue
        for entry in entries:
            # AF_LINK on macOS, AF_PACKET on Linux — both expose .address
            fam_name = getattr(entry.family, "name", str(entry.family))
            if "PACKET" in fam_name or "LINK" in fam_name:
                mac = (entry.address or "").lower()
                if re.fullmatch(r"[0-9a-f:]{17}", mac) and mac != "00:00:00:00:00:00":
                    out[iface] = mac
                    break
    return out


def _read_disks() -> list[dict]:
    """Return a list of {device, size_bytes} for fixed disks."""
    disks: list[dict] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return disks
    seen_devices: set[str] = set()
    for part in partitions:
        if not _is_real_disk(part):
            continue
        device = part.device
        if device in seen_devices:
            continue
        seen_devices.add(device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "size_bytes": int(usage.total),
            })
        except (PermissionError, OSError):
            continue
    return disks


def _detect_os_family(os_release: dict) -> str:
    """Map /etc/os-release ID/ID_LIKE into a coarse family bucket.

    Used by the server to derive ``os_family:debian`` style auto-tags.
    Returns ``""`` when we genuinely can't tell.
    """
    if platform.system() == "Darwin":
        return "macos"
    if platform.system() == "Windows":
        return "windows"
    raw_ids = (os_release.get("ID", "") + " " + os_release.get("ID_LIKE", "")).lower()
    if any(t in raw_ids for t in ("debian", "ubuntu")):
        return "debian"
    if any(t in raw_ids for t in ("rhel", "fedora", "centos", "rocky", "alma", "ol")):
        return "rhel"
    if "arch" in raw_ids:
        return "arch"
    if "alpine" in raw_ids:
        return "alpine"
    if "suse" in raw_ids:
        return "suse"
    return ""


def _normalize_arch(machine: str) -> str:
    """Map ``uname -m`` strings into amd64 / arm64 / arm / i386 / ``""``."""
    m = (machine or "").lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("arm"):
        return "arm"
    if m in ("i386", "i686", "x86"):
        return "i386"
    return m or ""


