"""Storage safety valve.

Vigil is a monitoring tool, so it should notice when its own metric store is
trending toward the disk limit — the failure mode that filled ``vigil_pgdata``
and took the stack down. This periodic task reports the database size (and the
metrics hypertable's share of it) and logs at WARNING/ERROR when it crosses
configurable soft/hard thresholds.

It reads sizes over SQL (``pg_database_size``), so it works from the Celery
worker without needing the Postgres data volume mounted. Thresholds:
    VIGIL_DB_SIZE_WARN_GB  (default 20) — log WARNING, likely growth problem.
    VIGIL_DB_SIZE_CRIT_GB  (default 40) — log ERROR, act now.
Set either to 0 to disable that threshold.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

_GB = 1024 ** 3


@shared_task(name="metrics.check_db_disk_usage")
def check_db_disk_usage():
    """Warn when the database is trending toward the disk limit."""
    if connection.vendor != "postgresql":
        return "check_db_disk_usage skipped (non-PostgreSQL backend)"

    warn_gb = float(getattr(settings, "VIGIL_DB_SIZE_WARN_GB", 20))
    crit_gb = float(getattr(settings, "VIGIL_DB_SIZE_CRIT_GB", 40))

    with connection.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        db_bytes = cur.fetchone()[0]

        metrics_bytes = None
        try:
            cur.execute("SELECT hypertable_size('metrics_metricpoint')")
            metrics_bytes = cur.fetchone()[0]
        except Exception:
            # Not a hypertable (yet) — fall back to plain relation size.
            try:
                cur.execute("SELECT pg_total_relation_size('metrics_metricpoint')")
                metrics_bytes = cur.fetchone()[0]
            except Exception:
                metrics_bytes = None

    db_gb = db_bytes / _GB
    metrics_gb = (metrics_bytes / _GB) if metrics_bytes is not None else None
    detail = f"database={db_gb:.1f}GB"
    if metrics_gb is not None:
        detail += f" metrics={metrics_gb:.1f}GB ({metrics_gb / db_gb * 100:.0f}% of db)"

    if crit_gb and db_gb >= crit_gb:
        logger.error(
            "Vigil metric store CRITICAL: %s exceeds hard cap %.0fGB. "
            "Disk exhaustion imminent — verify compression + retention policies "
            "are running (see docs/timescaledb-storage.md).",
            detail, crit_gb,
        )
        return f"CRITICAL {detail}"
    if warn_gb and db_gb >= warn_gb:
        logger.warning(
            "Vigil metric store WARNING: %s exceeds soft cap %.0fGB and is "
            "trending toward the disk limit.",
            detail, warn_gb,
        )
        return f"WARNING {detail}"

    logger.info("Vigil metric store OK: %s", detail)
    return f"OK {detail}"
