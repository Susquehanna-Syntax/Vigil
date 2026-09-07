from datetime import timedelta

from django.db.models import Avg, Case, F, IntegerField, Value, When
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping
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
      * ``overdue=1`` — only findings past their due date, not covered by
        an unexpired exception
      * ``due_within=<days>`` — findings due in the next N days; must be a
        positive integer under 3651 or the request is a 400

    ``sort`` is an explicit allowlist: ``due_date``, ``-due_date``,
    ``severity``, ``-severity``. Anything else is a 400 — the raw value is
    never interpolated into ``order_by()``. Findings without a due date
    sort last in both directions. ``severity`` ranks through
    ``SEVERITY_RANK`` (worst first), never the raw string column, where
    ``"medium" > "critical"`` alphabetically.
    """
    from django.utils.timezone import localdate

    qs = VulnFinding.objects.select_related("host", "exception")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    if scanner := request.query_params.get("scanner"):
        qs = qs.filter(scanner=scanner)
    if severity := request.query_params.get("severity"):
        qs = qs.filter(severity=severity)
    state = request.query_params.get("state", VulnFinding.State.OPEN)
    qs = qs.filter(state=state)

    today = localdate()

    if request.query_params.get("overdue") == "1":
        # Same definition as the finding's ``overdue`` property: past due,
        # still open, not covered by an unexpired exception.
        qs = qs.filter(
            state=VulnFinding.State.OPEN,
            due_date__lt=today,
        ).exclude(exception__expires_on__gte=today)

    due_within = request.query_params.get("due_within")
    if due_within is not None:
        # Validate before touching a timedelta: an unvalidated string here
        # would reach the ORM as a literal.
        if not due_within.isdigit() or not (1 <= int(due_within) < 3651):
            return Response(
                {"error": "due_within must be a positive integer under 3651"},
                status=400,
            )
        qs = qs.filter(
            due_date__gte=today,
            due_date__lte=today + timedelta(days=int(due_within)),
        )

    # severity is a string column ("medium" sorts above "critical"
    # alphabetically), so rank it numerically through SEVERITY_RANK.
    severity_rank = Case(
        *[When(severity=s, then=Value(r)) for s, r in SEVERITY_RANK.items()],
        default=Value(0),
        output_field=IntegerField(),
    )

    sort = request.query_params.get("sort")
    if sort is None:
        # Default: worst severity first, newest first within a tier.
        # The annotation is only needed for the default; the explicit
        # sorts use the rank as an expression directly.
        qs = qs.annotate(_severity_rank=severity_rank).order_by(
            "-_severity_rank", "-last_seen"
        )
    else:
        # Fixed field expression per allowed value — the parameter is only
        # ever a dict key, never part of the ordering itself.
        sort_map = {
            "due_date": [F("due_date").asc(nulls_last=True), F("id")],
            "-due_date": [F("due_date").desc(nulls_last=True), F("id")],
            "severity": [severity_rank.desc(), F("id")],
            "-severity": [severity_rank.asc(), F("id")],
        }
        if sort not in sort_map:
            return Response(
                {"error": "sort must be one of: due_date, -due_date, severity, -severity"},
                status=400,
            )
        qs = qs.order_by(*sort_map[sort])
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
    # Never fetched the host, so it never checked whether the caller may see
    # it — the score history of any machine was readable by id.
    _, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
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

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

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
