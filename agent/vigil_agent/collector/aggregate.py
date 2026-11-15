"""Aggregate scrape: run every metric collector and keep going when one breaks.

The named pairs and the per-collector ``except`` are the point: on a real host
one unusable counter (no PDH access for the monitor-mode service account) must
not drop the other categories' points with it.

Each category is taken through ``_common.shared``, that is from the *package*,
which is what lets a test patch one collector on ``vigil_agent.collector`` and
see this loop honour it — as it could when the loop lived in the single
collector module. While the package is still importing, ``shared`` yields None
and the locally imported function is used.
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
        ("collect_cpu", _common.shared("collect_cpu") or collect_cpu),
        ("collect_memory", _common.shared("collect_memory") or collect_memory),
        ("collect_disk", _common.shared("collect_disk") or collect_disk),
        ("collect_network", _common.shared("collect_network") or collect_network),
        ("collect_top_processes",
         lambda: (_common.shared("collect_top_processes") or collect_top_processes)(watch=watch)),
        ("collect_temperatures", _common.shared("collect_temperatures") or collect_temperatures),
        ("collect_gpu", lambda: (_common.shared("collect_gpu") or collect_gpu)(extended)),
    ]
    for name, fn in collectors:
        try:
            metrics.extend(fn())
        except Exception:
            _common.logger.exception("Collector %s failed", name)
    return metrics
