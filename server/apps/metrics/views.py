from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.hosts.models import Host

from .models import MetricPoint
from .serializers import MetricPointSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def metric_history(request, host_id, category, metric_name):
    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Host not found"}, status=404)

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
    total = qs.count()
    if total <= limit:
        points = list(qs[:limit])
    else:
        stride = total // limit + 1
        # The newest point is always included — a chart whose right edge lags
        # by up to `stride` samples looks stale even when it is current.
        points = list(qs)[::stride]
        if points and points[0] != qs.first():
            points.insert(0, qs.first())

    payload = MetricPointSerializer(points, many=True).data
    response = Response(payload)
    # Tells the caller the series is sampled, so a UI can say so rather than
    # implying it is showing every measurement.
    response["X-Vigil-Sampled"] = "1" if total > limit else "0"
    response["X-Vigil-Total-Points"] = str(total)
    return response