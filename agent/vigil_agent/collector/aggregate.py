"""Aggregate scrape: run every metric collector and keep going when one breaks.

The named pairs and the per-collector ``except`` are the point: on a real host
one unusable counter (no PDH access for the monitor-mode service account) must
not drop the other categories' points with it.
"""

from . import _common
from .basic import collect_cpu, collect_disk, collect_memory, collect_network
from .gpu import collect_gpu
from .processes import collect_top_processes
from .temperatures import collect_temperatures


def collect_all(config=None) -> list[dict]:
    """Collect all available system metrics.

    ``config`` is optional so existing callers and tests keep working; without
    it nothing is watched and GPU telemetry stays at the core set.
    """
    watch = list(getattr(config, "process_watch", None) or [])
    extended = bool(getattr(config, "gpu_extended", False))
    metrics = []
    # Named pairs rather than bare callables: two of these need arguments, and
    # a lambda would log "<lambda> failed" when one of them breaks.
    collectors = [
        ("collect_cpu", collect_cpu),
        ("collect_memory", collect_memory),
        ("collect_disk", collect_disk),
        ("collect_network", collect_network),
        ("collect_top_processes",
         lambda: collect_top_processes(watch=watch)),
        ("collect_temperatures", collect_temperatures),
        ("collect_gpu", lambda: collect_gpu(extended)),
    ]
    for name, fn in collectors:
        try:
            metrics.extend(fn())
        except Exception:
            _common.logger.exception("Collector %s failed", name)
    return metrics
