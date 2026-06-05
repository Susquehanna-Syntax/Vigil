from django.conf import settings
from django.db.models import Avg
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.hosts.models import Host

from .models import VulnFinding, VulnScan, VulnScoreHistory, VulnSummary
from .serializers import (
    VulnFindingSerializer,
    VulnScanSerializer,
    VulnScoreHistorySerializer,
    VulnSummarySerializer,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def vuln_list(request):
    """Return vulnerability summaries, optionally filtered by host."""
    # Worst-first: ascending score puts negatives before positives.
    qs = VulnSummary.objects.select_related("host").order_by("score", "-critical", "-high")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    return Response(VulnSummarySerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def fleet_score(request):
    """Return the fleet-wide score headline shown on the Vulns tab.

    ``score`` is the host-count-weighted average across every summary.
    ``worst`` points at the lowest-scored host so the UI can deep-link.
    """
    summaries = VulnSummary.objects.select_related("host")
    total = summaries.count()
    if total == 0:
        return Response({"score": 100, "host_count": 0, "worst": None})
    avg = summaries.aggregate(s=Avg("score"))["s"] or 100
    worst = summaries.order_by("score").first()
    return Response({
        "score": int(round(avg)),
        "host_count": total,
        "worst": {
            "host_id": str(worst.host_id),
            "hostname": worst.host.hostname,
            "score": worst.score,
        } if worst and worst.score < 100 else None,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def finding_list(request):
    """List vulnerability findings, filterable by host / scanner / severity / state.

    Query params (all optional):
      * ``host=<uuid>`` — limit to one host
      * ``scanner=nessus|greenbone|trivy``
      * ``severity=critical|high|medium|low|info``
      * ``state=open|fixed|suppressed`` (defaults to ``open``)
    """
    qs = VulnFinding.objects.select_related("host")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    if scanner := request.query_params.get("scanner"):
        qs = qs.filter(scanner=scanner)
    if severity := request.query_params.get("severity"):
        qs = qs.filter(severity=severity)
    state = request.query_params.get("state", VulnFinding.State.OPEN)
    qs = qs.filter(state=state)
    return Response(VulnFindingSerializer(qs[:500], many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def score_history(request, host_id):
    """Score history for one host — daily snapshots, newest first.

    Defaults to the last 30 days. Powers the sparkline on host detail.
    """
    days = int(request.query_params.get("days", 30))
    qs = VulnScoreHistory.objects.filter(host_id=host_id).order_by("-date")[:days]
    return Response(VulnScoreHistorySerializer(qs, many=True).data)


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
    launch happens in the next ``sync_vulns`` cycle (or inline if Celery
    is eager).

    v1 only queues Nessus scans from this endpoint; multi-scanner
    selection lands with PR #3 (Trivy) and PR #4 (Greenbone). The
    endpoint is therefore still gated on Nessus configuration.
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
        scanner=VulnScan.Scanner.NESSUS,
        target=host.ip_address or "",
        state=VulnScan.State.REQUESTED,
        requested_by=request.user,
        requested_via_task=False,
    )

    # Kick the sync task so the user doesn't wait an hour for the beat.
    try:
        from .tasks import sync_vulns
        sync_vulns.delay()
    except Exception:
        pass  # If broker is down, the next periodic run still picks it up.

    return Response(VulnScanSerializer(scan).data, status=201)
