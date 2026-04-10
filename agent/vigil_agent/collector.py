"""System metric collection using psutil.

Returns metric dicts ready for the checkin payload. Each metric is:
  {"category": str, "metric": str, "value": float, "labels": dict, "time": str}
"""

import logging
from datetime import datetime, timezone

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
