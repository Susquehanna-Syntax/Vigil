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

    return Response(MetricPointSerializer(qs[:limit], many=True).data)