"""CPU, memory, disk and network points.

Four small psutil walks that share one shape: every sub-call that can fail on a
locked-down host is guarded individually (``_warn_once``) so one broken counter
does not drop the whole category's points.
"""

from ._common import _point, _warn_once, psutil
from .disks import _is_real_disk


def collect_cpu() -> list[dict]:
    points = []
    per_cpu = psutil.cpu_percent(interval=1, percpu=True)
    for i, pct in enumerate(per_cpu):
        points.append(_point("cpu", "usage_percent", pct, {"core": str(i)}))
    points.append(_point("cpu", "usage_percent", psutil.cpu_percent(), {"core": "total"}))

    try:
        load_1, load_5, load_15 = psutil.getloadavg()
    except Exception as exc:
        _warn_once("loadavg", exc)
    else:
        points.append(_point("cpu", "load_1m", load_1))
        points.append(_point("cpu", "load_5m", load_5))
        points.append(_point("cpu", "load_15m", load_15))
    return points


def collect_memory() -> list[dict]:
    mem = psutil.virtual_memory()
    points = [
        _point("memory", "total_bytes", mem.total),
        _point("memory", "used_bytes", mem.used),
        _point("memory", "available_bytes", mem.available),
        _point("memory", "usage_percent", mem.percent),
    ]
    try:
        swap = psutil.swap_memory()
    except Exception as exc:
        _warn_once("swap", exc)
    else:
        points.extend([
            _point("memory", "swap_total_bytes", swap.total),
            _point("memory", "swap_used_bytes", swap.used),
            _point("memory", "swap_usage_percent", swap.percent),
        ])
    return points


def collect_disk() -> list[dict]:
    points = []
    seen_devices = set()
    for part in psutil.disk_partitions(all=False):
        if not _is_real_disk(part):
            continue
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


