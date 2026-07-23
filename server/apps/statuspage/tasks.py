"""Beat tasks for the status page — availability sampling and pruning.

``sample_uptime`` records one up/down reading per non-pending host each run;
the status page turns those into daily uptime bars. ``prune_old_uptime_samples``
keeps only the window the page renders (90 days) plus a little slack.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils.timezone import now

logger = logging.getLogger("vigil.statuspage")

# The status page renders 90 days of bars; keep a little more so day-boundary
# buckets are always complete.
RETENTION_DAYS = 95


@shared_task(name="statuspage.sample_uptime")
def sample_uptime():
    """Record the current up/down state of every non-pending host."""
    from apps.hosts.models import Host

    from .models import HostUptimeSample

    ts = now()
    hosts = Host.objects.exclude(status=Host.Status.PENDING).exclude(
        status=Host.Status.REJECTED)
    samples = [
        HostUptimeSample(host=h, time=ts, up=(h.status == Host.Status.ONLINE))
        for h in hosts
    ]
    HostUptimeSample.objects.bulk_create(samples)
    return f"sampled {len(samples)} hosts"


@shared_task(name="statuspage.prune_old_uptime_samples")
def prune_old_uptime_samples():
    """Drop uptime samples older than the rendered window."""
    from .models import HostUptimeSample

    cutoff = now() - timedelta(days=RETENTION_DAYS)
    deleted, _ = HostUptimeSample.objects.filter(time__lt=cutoff).delete()
    if deleted:
        logger.info("pruned %d old uptime samples", deleted)
    return f"pruned {deleted} samples"
