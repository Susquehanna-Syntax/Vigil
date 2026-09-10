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
