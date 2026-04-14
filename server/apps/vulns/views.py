from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import VulnSummary
from .serializers import VulnSummarySerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def vuln_list(request):
    """Return vulnerability summaries, optionally filtered by host."""
    qs = VulnSummary.objects.select_related("host").order_by("-critical", "-high", "-medium")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    return Response(VulnSummarySerializer(qs, many=True).data)
