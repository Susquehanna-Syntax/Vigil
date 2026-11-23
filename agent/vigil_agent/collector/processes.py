"""The top-N process ranking and the configured watch list.

``collect_top_processes`` is one function; it lives apart from the psutil
metric walks because its shape differs — a ranked sample plus per-name sums for
watched processes, so a quiet service keeps a chart of holes-free points.
"""

from ._common import _point, psutil


def collect_top_processes(n: int = 10, watch: list[str] | None = None) -> list[dict]:
    """Collect the top N processes by CPU and memory usage.

    ``watch`` names processes to report at every scrape whether or not they
    rank. The top-N alone cannot back a chart of one named service: a healthy,
    idle process drops out of the ranking and its history goes to holes exactly
    when it is behaving. Watched rows are marked ``watched=1`` so the server
    can tell a guaranteed sample from an incidental one.
    """
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

    # Watched processes. Several processes can share a name (workers, tabs), and
    # a chart of one of them at random is a lie — the sample is the sum across
    # every live process of that name, with the count alongside it.
    for wanted in watch or []:
        matches = [p for p in procs if (p["name"] or "") == wanted]
        labels = {"name": wanted, "watched": "1"}
        points.append(_point(
            "process", "cpu_percent", sum(p["cpu_percent"] or 0 for p in matches), labels))
        points.append(_point(
            "process", "memory_percent", sum(p["memory_percent"] or 0 for p in matches), labels))
        # Zero is the useful answer when a watched process is not running: it
        # is how the chart shows an outage rather than simply stopping.
        points.append(_point("process", "instances", len(matches), labels))

    return points
