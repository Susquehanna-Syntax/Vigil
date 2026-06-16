from django.db.models import Avg, Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.hosts.models import Host

from .models import VulnFinding, VulnScan, VulnScoreHistory, VulnSummary
from .scoring import SEVERITY_RANK
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

    # Worst-first. severity is a string column ("medium" sorts above
    # "critical" alphabetically), so rank it numerically — this also
    # guarantees criticals survive the 500-row cap.
    severity_rank = Case(
        *[When(severity=s, then=Value(r)) for s, r in SEVERITY_RANK.items()],
        default=Value(0),
        output_field=IntegerField(),
    )
    qs = qs.annotate(_severity_rank=severity_rank).order_by(
        "-_severity_rank", "-last_seen"
    )
    return Response(VulnFindingSerializer(qs[:500], many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def score_history(request, host_id):
    """Score history for one host — daily snapshots, newest first.

    Defaults to the last 30 days. Powers the sparkline on host detail.
    """
    try:
        days = int(request.query_params.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
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
    """Queue a network vulnerability scan for ``host_id``.

    2FA-gated (matches task deploy). Returns 409 if there's already an
    active scan for this host — one in-flight scan per host. The actual
    launch happens in the next ``sync_vulns`` cycle (or inline if Celery
    is eager).

    Optional body field ``scanner`` selects the engine (``nessus`` or
    ``greenbone``). When omitted, the first configured engine wins, in
    that order — so Nessus-only and Greenbone-only installs both work
    without the UI having to know which is set up.
    """
    from apps.accounts.totp import require_totp_confirmation

    from .scanners import SCANNER_REGISTRY

    host = get_object_or_404(Host, pk=host_id)

    network_engines = (VulnScan.Scanner.NESSUS, VulnScan.Scanner.GREENBONE)
    requested = (request.data.get("scanner") or "").strip().lower()
    if requested:
        if requested not in network_engines:
            return Response(
                {"error": f"scanner must be one of: {', '.join(network_engines)}"},
                status=400,
            )
        if not SCANNER_REGISTRY[requested]().configured():
            return Response(
                {"error": f"{requested} is not configured on this server"},
                status=503,
            )
        engine = requested
    else:
        engine = next(
            (e for e in network_engines if SCANNER_REGISTRY[e]().configured()),
            None,
        )
        if engine is None:
            return Response(
                {"error": "No network scanner (Nessus or Greenbone) is configured"},
                status=503,
            )

    error = require_totp_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=401)

    # One active scan per host — block duplicates regardless of engine;
    # two scanners hammering the same box at once helps nobody.
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
        scanner=engine,
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
