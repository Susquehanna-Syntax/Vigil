"""Periodic vulnerability sync.

This module is now a thin Celery wrapper around
:data:`apps.vulns.scanners.SCANNER_REGISTRY`. Each implementation
(Nessus today; Greenbone and Trivy in upcoming PRs) owns its own
launch/poll/ingest logic — this file only schedules the cycle.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils.timezone import localdate

from .models import VulnScoreHistory, VulnSummary
from .scanners import SCANNER_REGISTRY

logger = logging.getLogger(__name__)


@shared_task(name="vulns.sync_vulns")
def sync_vulns() -> str:
    """Run one sync cycle across every configured scanner.

    For each scanner in :data:`SCANNER_REGISTRY` the task:

      * instantiates it,
      * skips it silently when ``configured()`` is False (no env vars),
      * calls ``sync()`` and records the status,
      * isolates exceptions so one bad scanner can't break the rest.

    Returns a single ``"scanner1: status | scanner2: status"`` line for
    the Celery result log.
    """
    parts: list[str] = []
    for name, cls in SCANNER_REGISTRY.items():
        scanner = cls()
        if not scanner.configured():
            parts.append(f"{name}: not configured")
            continue
        try:
            status = scanner.sync()
        except Exception as exc:
            logger.exception("Scanner %s crashed during sync", name)
            status = f"error: {exc}"
        parts.append(f"{name}: {status}")
    return " | ".join(parts) or "no scanners registered"


@shared_task(name="vulns.sync_nessus_vulns")
def sync_nessus_vulns() -> str:
    """Deprecated alias for :func:`sync_vulns`.

    Kept for one release so the old beat schedule name and any pinned
    ``.delay()`` callers keep working. Remove after the next major bump.
    """
    return sync_vulns()


@shared_task(name="vulns.snapshot_scores")
def snapshot_scores() -> str:
    """Write one :class:`VulnScoreHistory` row per host for today.

    Powers the score sparkline + trend arrow. Idempotent via the
    ``(host, date)`` unique constraint — running it twice in one day
    just updates the latest values rather than duplicating.

    Hosts with no :class:`VulnSummary` (never been scanned) are
    skipped; we don't want to draw flat-100 lines for hosts that have
    no scanner data at all.
    """
    today = localdate()
    written = 0
    for summary in VulnSummary.objects.select_related("host").all():
        VulnScoreHistory.objects.update_or_create(
            host=summary.host,
            date=today,
            defaults={"score": summary.score},
        )
        written += 1
    return f"snapshotted {written} host score(s) for {today}"


@shared_task(name="vulns.refresh_kev")
def refresh_kev() -> str:
    """Pull a fresh CISA KEV catalogue, if the operator enabled it.

    A no-op when ``VIGIL_KEV_LIVE_REFRESH`` is off, and non-raising in every
    failure mode — the bundled snapshot stays in place and scoring is
    unaffected.
    """
    from . import kev

    written = kev.fetch_live()
    return f"refreshed {written} KEV entries"


@shared_task(name="vulns.prune_old_score_history")
def prune_old_score_history() -> str:
    """Drop score snapshots past their retention window.

    The model deferred this to "a future maintenance task". At one row per host
    per day the deferral was sound — it is not a table that will surprise
    anyone — but an unbounded table with no owner is how a small number becomes
    a large one three years later, and the task costs almost nothing.
    """
    from django.conf import settings
    from django.utils.timezone import now

    days = int(getattr(settings, "VIGIL_SCORE_HISTORY_RETENTION_DAYS", 730))
    if days <= 0:
        return "Score-history retention disabled"
    cutoff = (now() - timedelta(days=days)).date()
    deleted, _ = VulnScoreHistory.objects.filter(date__lt=cutoff).delete()
    if deleted:
        logger.info("Pruned %d vulnerability score snapshot(s) older than %d days",
                    deleted, days)
    return f"Pruned {deleted} score snapshot(s) older than {days} days"
