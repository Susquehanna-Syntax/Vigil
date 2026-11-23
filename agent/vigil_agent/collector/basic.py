"""CPU, memory, disk and network points.

Four small psutil walks that share one shape: every sub-call that can fail on a
locked-down host is guarded individually (``_warn_once``) so one broken counter
does not drop the whole category's points.
"""

import threading
import time

from ._common import _point, _warn_once, psutil
from .disks import _is_real_disk


#: The thread whose psutil CPU baseline is set, and when it was last read.
#: psutil keeps one baseline per thread, so a baseline taken on another thread
#: (the import, under the Windows service host) is no baseline here.
_cpu_baseline: tuple[int, float] | None = None
#: Below this, the time since the last reading is too short to average over.
_MIN_CPU_WINDOW = 1.0


def _cpu_usage() -> tuple[list[float], float]:
    """(per core, total) CPU use, as the average since the previous call.

    A check-in used to sleep a second to sample the cores, and then reported a
    total measured over a different window (the whole interval). Both are now
    the average over the interval between check-ins: a burst between two
    samples is in the number instead of missed, the two agree, and the
    check-in no longer waits. Only a first reading, with nothing to compare
    against, still takes the one-second sample.
    """
    global _cpu_baseline
    thread, now = threading.get_ident(), time.monotonic()
    fresh = (_cpu_baseline is None or _cpu_baseline[0] != thread
             or now - _cpu_baseline[1] < _MIN_CPU_WINDOW)
    if fresh:
        per_cpu = psutil.cpu_percent(interval=1, percpu=True)
        psutil.cpu_percent()  # set the total's baseline for the next call
        total = round(sum(per_cpu) / len(per_cpu), 1) if per_cpu else 0.0
    else:
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        total = psutil.cpu_percent(interval=None)
    _cpu_baseline = (thread, time.monotonic())
    return per_cpu, total


def collect_cpu() -> list[dict]:
    points = []
    per_cpu, total = _cpu_usage()
    for i, pct in enumerate(per_cpu):
        points.append(_point("cpu", "usage_percent", pct, {"core": str(i)}))
    points.append(_point("cpu", "usage_percent", total, {"core": "total"}))

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


