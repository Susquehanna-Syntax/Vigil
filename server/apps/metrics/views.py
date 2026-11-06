import re
import uuid
from collections import deque
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping
from apps.hosts.models import Host

from .models import MetricPoint
from .serializers import MetricPointSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def metric_history(request, host_id, category, metric_name):
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

    qs = MetricPoint.objects.filter(
        host=host, category=category, metric=metric_name
    ).order_by("-time")

    # Query params are untrusted — a malformed limit or timestamp must
    # come back as a 400, not an unhandled 500.
    try:
        limit = int(request.query_params.get("limit", 200))
    except (TypeError, ValueError):
        return Response({"error": "limit must be an integer"}, status=400)
    limit = max(1, min(limit, 1000))

    for param, lookup in (("from", "time__gte"), ("to", "time__lte")):
        raw = request.query_params.get(param)
        if not raw:
            continue
        ts = parse_datetime(raw)
        if ts is None:
            return Response(
                {"error": f"{param} must be an ISO-8601 datetime"}, status=400
            )
        qs = qs.filter(**{lookup: ts})

    # Sample across the whole window rather than truncating to the newest N.
    #
    # `qs[:limit]` looked reasonable but silently broke the time-range buttons:
    # ordering is newest-first, so a 7-day request over minute-resolution
    # metrics returned the most recent 500 points — about eight hours. 24h and
    # 7d rendered near-identical charts and the buttons looked inert.
    #
    # Stride sampling rather than bucket averaging: averaging would smooth away
    # exactly the spikes a monitoring chart exists to show, and inventing a
    # value that was never measured is the wrong trade for this tool. Sampling
    # can miss a spike, but every point it returns is one that really happened.
    # `limit=1` means "the latest reading", and the answer is the first row of
    # an index the database already has. It used to fall into the sampling
    # branch below — because total > 1 is true for any host that has ever
    # reported twice — and materialize the entire series to return points[0].
    # The Disk Pressure widget asks exactly this, once per host, in parallel,
    # on every 15-second poll: at 50 hosts that was 200 whole-series fetches a
    # minute to read 50 numbers.
    if limit == 1:
        newest = qs.first()
        points = [newest] if newest else []
        total = qs.count() if newest else 0
    else:
        total = qs.count()
        if total <= limit:
            points = list(qs[:limit])
        else:
            stride = total // limit + 1
            # Stride over primary keys streamed from the database rather than
            # over model instances held in memory. `list(qs)` pulled every
            # matching row — a year for one host is tens of millions, each with
            # a JSON labels column — into the worker just to throw away all but
            # a thousand of them. The keys arrive in chunks and only the ones
            # actually wanted are kept, so peak memory is the sample size
            # rather than the series size.
            wanted = [
                pk for i, pk in enumerate(
                    qs.values_list("pk", flat=True).iterator(chunk_size=5000))
                if i % stride == 0
            ][:limit]
            # Re-read in the same order the caller expects; `pk__in` does not
            # preserve ordering on its own.
            by_pk = {p.pk: p for p in qs.filter(pk__in=wanted)}
            points = [by_pk[pk] for pk in wanted if pk in by_pk]
            # The newest point is always included — a chart whose right edge
            # lags by up to `stride` samples looks stale even when it is
            # current.
            newest = qs.first()
            if newest and (not points or points[0].pk != newest.pk):
                points.insert(0, newest)

    payload = MetricPointSerializer(points, many=True).data
    response = Response(payload)
    # Tells the caller the series is sampled, so a UI can say so rather than
    # implying it is showing every measurement.
    response["X-Vigil-Sampled"] = "1" if total > limit else "0"
    response["X-Vigil-Total-Points"] = str(total)
    return response

#: How far back the catalogue looks. Bounded on purpose — an unbounded DISTINCT
#: over the metrics table is a scan of the one table that grows without limit —
#: but a week rather than a day: a host that was off over a weekend should not
#: drop out of the picker, taking its metrics with it.
CATALOG_WINDOW_HOURS = 24 * 7


#: What the agent's collector emits, as (category, metric). The single source
#: of truth for that claim — the widget-defaults test asserts against this, and
#: it is the fallback below. Keep in step with agent/vigil_agent/collector.py.
COLLECTOR_METRICS = (
    ("cpu", "usage_percent"),
    ("cpu", "load_1m"),
    ("memory", "usage_percent"),
    ("memory", "swap_usage_percent"),
    ("disk", "usage_percent"),
    ("network", "bytes_sent"),
    ("network", "bytes_recv"),
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def metric_catalog(request):
    """The category/metric pairs this fleet is actually reporting.

    Exists so a picker can only offer metrics that exist. Offering a name
    nothing reports produces a widget that draws an empty window and reads as
    broken rather than as misconfigured — which is precisely how the dashboard
    widgets' own defaults shipped wrong the first time.
    """
    from datetime import timedelta

    from django.utils.timezone import now

    since = now() - timedelta(hours=CATALOG_WINDOW_HOURS)
    pairs = (MetricPoint.objects
             .filter(time__gte=since)
             .values_list("category", "metric")
             .distinct()
             .order_by("category", "metric"))
    rows = list(pairs)
    if not rows:
        # A fleet that has not reported for a while — a fresh install, or every
        # agent offline. Offering nothing reads as a broken picker, so fall back
        # to what the collector emits; the operator can still pick sensibly and
        # live data takes precedence the moment any arrives.
        rows = list(COLLECTOR_METRICS)
    return Response([
        {"category": category, "metric": metric, "label": f"{category} / {metric}"}
        for category, metric in rows
    ])


#: Batched-recent limits. The endpoint exists to replace a per-host fan-out, so
#: it is bounded: eight series, fifty points per label set, one day back, a
#: thousand hosts per request. Beyond that the caller wants a chart, which is
#: the per-host endpoint's job.
MAX_RECENT_SERIES = 8
MAX_RECENT_POINTS = 50
MAX_RECENT_HOSTS = 1000
MAX_RECENT_LABEL_SETS = 128
#: What one answer may hold, whatever an agent chose to label its points with:
#: points per host per series (label sets x N), and points in all.
MAX_RECENT_POINTS_PER_HOST_SERIES = 200
MAX_RECENT_POINTS_TOTAL = 100_000
MAX_RECENT_WINDOW = timedelta(hours=24)

_RECENT_SERIES_PART = re.compile(r"^[a-z0-9_]{1,50}$")


def _parse_recent_series(raw):
    """([("cpu", "usage_percent", 5), ...], None) or (None, "<what is wrong>")."""
    if not raw:
        return None, "series is required: comma-separated category/metric:N"
    entries = []
    for piece in raw.split(","):
        category, slash, rest = piece.partition("/")
        metric, colon, count = rest.partition(":")
        if not slash or not colon:
            return None, (
                f"each series must be category/metric:N with N between 1 and "
                f"{MAX_RECENT_POINTS}; got {piece!r}"
            )
        if not count.isdigit() or not 1 <= int(count) <= MAX_RECENT_POINTS:
            return None, (
                f"the point count in series {piece!r} must be an integer between "
                f"1 and {MAX_RECENT_POINTS}"
            )
        if not _RECENT_SERIES_PART.match(category) or not _RECENT_SERIES_PART.match(metric):
            return None, (
                f"the category and metric in series {piece!r} must each match "
                f"[a-z0-9_]{{1,50}}"
            )
        entries.append((category, metric, int(count)))
    if len(entries) > MAX_RECENT_SERIES:
        return None, f"at most {MAX_RECENT_SERIES} series per request"
    return entries, None


def _label_key(labels):
    return tuple(sorted((labels or {}).items())) if isinstance(labels, dict) else (str(labels),)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def metrics_recent(request):
    """The recent points of several series for every visible host at once.

    Replaces the hosts page's five-requests-per-card and the Disk Pressure
    widget's one-per-host: the whole fleet in one query per requested series.

    `category/metric:N` keeps the newest N points of each label set (each CPU
    core, each mount, each interface), not N points in all: a card wants the
    latest reading of every mount and the last two of every interface, and a
    plain newest-N would hand a 16-core host sixteen per-core points and no
    total. Hosts are named with `host=` (at most a thousand); without it the
    answer covers the first thousand visible hosts by name, and says so with
    an `X-Vigil-Truncated: 1` header when there are more, or when the
    request's point budget cut the answer short.
    """
    entries, problem = _parse_recent_series(request.query_params.get("series"))
    if problem:
        return Response({"error": problem}, status=400)

    raw_from = request.query_params.get("from")
    from_ts = parse_datetime(raw_from) if raw_from else None
    if from_ts is None:
        return Response({"error": "from is required and must be an ISO-8601 datetime"},
                        status=400)

    now = timezone.now()
    if from_ts < now - MAX_RECENT_WINDOW:
        return Response(
            {"error": f"from may not be older than "
                      f"{int(MAX_RECENT_WINDOW.total_seconds() // 3600)} hours; "
                      "this endpoint is for recent points"},
            status=400,
        )

    # The request's own scope: a site admin gets their sites' hosts, and a host
    # they named but cannot see stays silently absent rather than confirming it.
    # The cap applies to what was asked for, never before it: capping the whole
    # fleet first would silently drop named hosts past the thousandth.
    wanted = request.query_params.getlist("host")
    truncated = False
    if wanted:
        if len(wanted) > MAX_RECENT_HOSTS:
            return Response({"error": f"at most {MAX_RECENT_HOSTS} host= per request"},
                            status=400)
        valid = set()
        for raw in wanted:
            try:
                valid.add(uuid.UUID(raw))
            except ValueError:
                return Response({"error": f"host must be a host id; got {raw[:40]!r}"},
                                status=400)
        ids = list(Host.objects.filter(id__in=valid).values_list("id", flat=True))
    else:
        ids = list(Host.objects.order_by("hostname")
                   .values_list("id", flat=True)[:MAX_RECENT_HOSTS + 1])
        truncated = len(ids) > MAX_RECENT_HOSTS
        ids = ids[:MAX_RECENT_HOSTS]

    out: dict = {}
    total = 0
    for category, metric, count in entries:
        # Label sets x N stays within the per-host budget: a host reporting
        # 128 mounts gets 200 points at N=1, but only 100 interfaces at N=2.
        max_sets = max(1, min(MAX_RECENT_LABEL_SETS, MAX_RECENT_POINTS_PER_HOST_SERIES // count))
        key = f"{category}/{metric}"
        # One ordered scan over the bounded window, keeping the newest `count`
        # of each label set per host, rather than a query per host.
        rows = (MetricPoint.objects
                .filter(host_id__in=ids, category=category, metric=metric,
                        time__gte=from_ts)
                .order_by("host_id", "time")
                .values("host_id", "time", "value", "labels"))
        kept: dict = {}
        for row in rows.iterator(chunk_size=1000):
            host_key = str(row["host_id"])
            per_host = kept.get(host_key)
            label_key = _label_key(row["labels"])
            seen = per_host.get(label_key) if per_host is not None else None
            if seen is None or len(seen) < count:
                # A point that grows the answer, rather than replacing an older
                # one in a full deque, counts against the request's budget, and
                # is refused before anything is allocated for it. (Label size
                # is bounded at ingest, _bounded_labels, so points are.)
                if total >= MAX_RECENT_POINTS_TOTAL:
                    truncated = True
                    continue
                if seen is None:
                    if per_host is None:
                        per_host = kept[host_key] = {}
                    elif len(per_host) >= max_sets:
                        continue
                    seen = per_host[label_key] = deque(maxlen=count)
                total += 1
            seen.append({"time": row["time"].isoformat(),
                         "value": row["value"],
                         "labels": row["labels"]})
        for host_key, by_label in kept.items():
            points = [p for dq in by_label.values() for p in dq]
            points.sort(key=lambda p: p["time"], reverse=True)
            out.setdefault(host_key, {})[key] = points
    response = Response(out)
    if truncated:
        response["X-Vigil-Truncated"] = "1"
    return response
