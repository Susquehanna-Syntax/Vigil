"""Bucketed metric history: one point per hour or per day with avg, min, max.

PostgreSQL with TimescaleDB reads the continuous aggregates; the newest part
of the range, which the aggregate has not materialized yet, is computed from
raw rows. Everything else computes the whole range from raw rows.
"""
from datetime import UTC, timedelta

from django.db import connection
from django.db.models import Avg, Max, Min
from django.db.models.functions import TruncDay, TruncHour

from .models import MetricPoint

RAW_MAX = timedelta(hours=24)
HOURLY_MAX = timedelta(days=30)

#: Bucket name -> (view, size, how far back from now the view may lag).
BUCKETS = {
    "1h": ("metrics_metricpoint_hourly", timedelta(hours=1), timedelta(hours=3)),
    "1d": ("metrics_metricpoint_daily", timedelta(days=1), timedelta(days=3)),
}

_views_present = None


def pick_bucket(start, end) -> str:
    span = end - start
    if span <= RAW_MAX:
        return "raw"
    return "1h" if span <= HOURLY_MAX else "1d"


def has_rollups() -> bool:
    """Do the continuous aggregates exist? Asked once per process."""
    global _views_present
    if _views_present is None:
        if connection.vendor != "postgresql":
            _views_present = False
        else:
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass('metrics_metricpoint_daily') IS NOT NULL")
                _views_present = bool(cur.fetchone()[0])
    return _views_present


def _floor(moment, size):
    moment = moment.astimezone(UTC)
    if size >= timedelta(days=1):
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(minute=0, second=0, microsecond=0)


def _from_raw(host_id, category, metric, start, end, size) -> list:
    trunc = TruncDay if size >= timedelta(days=1) else TruncHour
    rows = (MetricPoint.objects
            .filter(host_id=host_id, category=category, metric=metric, time__gte=start, time__lt=end)
            .annotate(bucket=trunc("time", tzinfo=UTC))
            .values("bucket", "labels")
            .annotate(avg=Avg("value"), low=Min("value"), high=Max("value"))
            .order_by("-bucket"))
    return [{"time": r["bucket"], "value": r["avg"], "min": r["low"], "max": r["high"],
             "labels": r["labels"]} for r in rows]


def _from_view(view, host_id, category, metric, start, end) -> list:
    with connection.cursor() as cur:
        cur.execute(
            # view is one of the two BUCKETS constants, never caller input
            f"SELECT bucket, avg_value, min_value, max_value, labels FROM {view} "
            "WHERE host_id = %s AND category = %s AND metric = %s AND bucket >= %s AND bucket < %s "
            "ORDER BY bucket DESC",
            [host_id, category, metric, start, end])
        return [{"time": b, "value": avg, "min": low, "max": high, "labels": labels}
                for b, avg, low, high, labels in cur.fetchall()]


def history(host_id, category, metric, start, end, bucket) -> list:
    """Newest first, one row per bucket and label set."""
    view, size, lag = BUCKETS[bucket]
    start = _floor(start, size)
    if not has_rollups():
        return _from_raw(host_id, category, metric, start, end, size)
    boundary = max(start, _floor(end - lag, size))
    newest = _from_raw(host_id, category, metric, boundary, end, size)
    older = _from_view(view, host_id, category, metric, start, boundary) if boundary > start else []
    return newest + older
