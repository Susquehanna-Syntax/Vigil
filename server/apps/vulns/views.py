from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.hosts.models import Host

from .models import VulnScan, VulnSummary
from .serializers import VulnScanSerializer, VulnSummarySerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def vuln_list(request):
    """Return vulnerability summaries, optionally filtered by host."""
    qs = VulnSummary.objects.select_related("host").order_by("-critical", "-high", "-medium")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    return Response(VulnSummarySerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def scan_list(request):
    """Return recent scan requests / runs, newest first.

    Optionally filter by host with ``?host=<uuid>``. Capped at 100 rows.
    """
    qs = VulnScan.objects.select_related("host", "requested_by")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    return Response(VulnScanSerializer(qs[:100], many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def scan_create(request, host_id):
    """Queue a Nessus scan for ``host_id``.

    2FA-gated (matches task deploy). Returns 409 if there's already an
    active scan for this host — one in-flight scan per host. The actual
    Nessus launch happens in the next ``sync_nessus_vulns`` cycle (or
    inline if Celery is eager).
    """
    from apps.accounts.totp import require_totp_confirmation

    host = get_object_or_404(Host, pk=host_id)

    # Nessus must be configured before we accept scan requests.
    if not all([
        getattr(settings, "NESSUS_URL", ""),
        getattr(settings, "NESSUS_ACCESS_KEY", ""),
        getattr(settings, "NESSUS_SECRET_KEY", ""),
    ]):
        return Response(
            {"error": "Nessus is not configured on this server"}, status=503
        )

    error = require_totp_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=401)

    # One active scan per host — block duplicates.
    active = VulnScan.objects.filter(
        host=host,
        state__in=[
            VulnScan.State.REQUESTED,
            VulnScan.State.LAUNCHED,
            VulnScan.State.RUNNING,
        ],
    ).first()
    if active:
        return Response(
            {"error": "Host already has an active scan", "active_scan_id": str(active.id)},
            status=409,
        )

    scan = VulnScan.objects.create(
        host=host,
        target=host.ip_address or "",
        state=VulnScan.State.REQUESTED,
        requested_by=request.user,
        requested_via_task=False,
    )

    # Kick the sync task so the user doesn't wait an hour for the beat.
    try:
        from .tasks import sync_nessus_vulns
        sync_nessus_vulns.delay()
    except Exception:
        pass  # If broker is down, the next periodic run still picks it up.

    return Response(VulnScanSerializer(scan).data, status=201)
