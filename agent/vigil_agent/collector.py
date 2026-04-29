"""System metric collection using psutil.

Returns metric dicts ready for the checkin payload. Each metric is:
  {"category": str, "metric": str, "value": float, "labels": dict, "time": str}
"""

import logging
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psutil

logger = logging.getLogger("vigil.collector")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _point(category: str, metric: str, value: float, labels: dict | None = None) -> dict:
    return {
        "category": category,
        "metric": metric,
        "value": round(value, 2),
        "labels": labels or {},
        "time": _now_iso(),
    }


def collect_cpu() -> list[dict]:
    points = []
    per_cpu = psutil.cpu_percent(interval=1, percpu=True)
    for i, pct in enumerate(per_cpu):
        points.append(_point("cpu", "usage_percent", pct, {"core": str(i)}))
    points.append(_point("cpu", "usage_percent", psutil.cpu_percent(), {"core": "total"}))

    load_1, load_5, load_15 = psutil.getloadavg()
    points.append(_point("cpu", "load_1m", load_1))
    points.append(_point("cpu", "load_5m", load_5))
    points.append(_point("cpu", "load_15m", load_15))
    return points


def collect_memory() -> list[dict]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return [
        _point("memory", "total_bytes", mem.total),
        _point("memory", "used_bytes", mem.used),
        _point("memory", "available_bytes", mem.available),
        _point("memory", "usage_percent", mem.percent),
        _point("memory", "swap_total_bytes", swap.total),
        _point("memory", "swap_used_bytes", swap.used),
        _point("memory", "swap_usage_percent", swap.percent),
    ]


def collect_disk() -> list[dict]:
    points = []
    seen_devices = set()
    for part in psutil.disk_partitions(all=False):
        if part.device in seen_devices:
            continue
        seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        labels = {"mount": part.mountpoint, "device": part.device, "fstype": part.fstype}
        points.append(_point("disk", "total_bytes", usage.total, labels))
        points.append(_point("disk", "used_bytes", usage.used, labels))
        points.append(_point("disk", "usage_percent", usage.percent, labels))
    return points


def collect_network() -> list[dict]:
    points = []
    counters = psutil.net_io_counters(pernic=True)
    for iface, stats in counters.items():
        if iface == "lo":
            continue
        labels = {"interface": iface}
        points.append(_point("network", "bytes_sent", stats.bytes_sent, labels))
        points.append(_point("network", "bytes_recv", stats.bytes_recv, labels))
        points.append(_point("network", "errors_in", stats.errin, labels))
        points.append(_point("network", "errors_out", stats.errout, labels))
        points.append(_point("network", "drops_in", stats.dropin, labels))
        points.append(_point("network", "drops_out", stats.dropout, labels))
    return points


def collect_top_processes(n: int = 10) -> list[dict]:
    """Collect the top N processes by CPU and memory usage."""
    points = []
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if info["pid"] == 0:
                continue
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Top by CPU
    by_cpu = sorted(procs, key=lambda p: p["cpu_percent"] or 0, reverse=True)[:n]
    for rank, p in enumerate(by_cpu):
        labels = {"pid": str(p["pid"]), "name": p["name"] or "unknown", "rank": str(rank)}
        points.append(_point("process", "cpu_percent", p["cpu_percent"] or 0, labels))

    # Top by memory
    by_mem = sorted(procs, key=lambda p: p["memory_percent"] or 0, reverse=True)[:n]
    for rank, p in enumerate(by_mem):
        labels = {"pid": str(p["pid"]), "name": p["name"] or "unknown", "rank": str(rank)}
        points.append(_point("process", "memory_percent", p["memory_percent"] or 0, labels))

    return points


def collect_all() -> list[dict]:
    """Collect all available system metrics."""
    metrics = []
    collectors = [collect_cpu, collect_memory, collect_disk, collect_network, collect_top_processes]
    for fn in collectors:
        try:
            metrics.extend(fn())
        except Exception:
            logger.exception("Collector %s failed", fn.__name__)
    return metrics


# ── Inventory collection ────────────────────────────────────────────────────
#
# Inventory is collected once and shipped alongside the regular metric
# payload. It moves at human timescales (hardware doesn't change often), so
# the runtime can choose to send it on a slower cadence than metrics.

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
                )
                if proc.returncode == 0:
                    inv[field] = proc.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                continue

    inv["mac_addresses"] = _read_mac_addresses()
    inv["disks"] = _read_disks()

    return inv
