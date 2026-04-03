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

    from_ts = request.query_params.get("from")
    to_ts = request.query_params.get("to")
    limit = min(int(request.query_params.get("limit", 200)), 1000)

    if from_ts:
        qs = qs.filter(time__gte=from_ts)
    if to_ts:
        qs = qs.filter(time__lte=to_ts)

    return Response(MetricPointSerializer(qs[:limit], many=True).data)