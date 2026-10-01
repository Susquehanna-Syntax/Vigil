"""The fleet view by update: what is missing, where, for how long — and the
fleet-wide approve / decline every policy reads."""

from django.db.models import Count, Max, Min, Q
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from vigil import scoping

from apps.accounts.permissions import IsAdmin

from .models import PendingUpdate, UpdateDecision

MAX_ROWS = 1000


def _scoped(user):
    return scoping.filter_by_site(
        PendingUpdate.objects.exclude(host__status="rejected"), user, path="host__")


def _decisions() -> dict:
    return {(d.kind, d.key): d for d in UpdateDecision.objects.select_related("decided_by")}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def update_list(request):
    """One row per update (Windows KB / Linux package), worst-aged first."""
    qs = _scoped(request.user)
    kind = request.query_params.get("kind", "").strip()
    if kind:
        qs = qs.filter(kind=kind)
    q = request.query_params.get("q", "").strip()
    if q:
        qs = qs.filter(Q(key__icontains=q) | Q(title__icontains=q))
    severity = request.query_params.get("severity", "").strip()
    if severity:
        qs = qs.filter(severity=severity)
    grouped = (qs.values("kind", "key")
               .annotate(hosts=Count("host", distinct=True), oldest=Min("first_seen"),
                         title=Max("title"), severity=Max("severity"),
                         classification=Max("classification"),
                         reboot=Max("reboot_required"))
               .order_by("oldest", "key"))
    decisions = _decisions()
    wanted = request.query_params.get("decision", "").strip()
    stamp = now()
    rows = []
    for g in grouped:
        decision = decisions.get((g["kind"], g["key"]))
        state = decision.decision if decision else "undecided"
        if wanted and state != wanted:
            continue
        rows.append({
            "kind": g["kind"], "key": g["key"], "title": g["title"],
            "severity": g["severity"], "classification": g["classification"],
            "reboot_required": bool(g["reboot"]), "hosts": g["hosts"],
            "oldest_first_seen": g["oldest"].isoformat(),
            "age_days": (stamp - g["oldest"]).days,
            "decision": state,
            "decided_by": decision.decided_by.username if decision and decision.decided_by else None,
        })
        if len(rows) >= MAX_ROWS:
            break
    return Response({"count": len(rows), "results": rows})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def update_hosts(request):
    """The hosts missing one update — ``?kind=windows&key=KB5034441``."""
    kind = request.query_params.get("kind", "")
    key = request.query_params.get("key", "")
    stamp = now()
    rows = (_scoped(request.user).filter(kind=kind, key=key)
            .select_related("host").order_by("first_seen"))
    return Response({"results": [{
        "host_id": str(p.host_id), "hostname": p.host.hostname,
        "first_seen": p.first_seen.isoformat(), "age_days": (stamp - p.first_seen).days,
        "version": p.version,
    } for p in rows[:MAX_ROWS]]})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def update_decide(request):
    """``{kind, keys: [...], decision: approved|declined|clear}`` — fleet-wide."""
    kind = request.data.get("kind")
    keys = request.data.get("keys")
    decision = request.data.get("decision")
    if kind not in PendingUpdate.Kind.values:
        return Response({"detail": "kind must be windows or linux"},
                        status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(keys, list) or not keys or not all(
            isinstance(k, str) and 0 < len(k.strip()) <= 300 for k in keys):
        return Response({"detail": "keys must be a non-empty list of update keys"},
                        status=status.HTTP_400_BAD_REQUEST)
    keys = [k.strip() for k in keys]
    if decision == "clear":
        UpdateDecision.objects.filter(kind=kind, key__in=keys).delete()
        return Response({"cleared": len(keys)})
    if decision not in UpdateDecision.Decision.values:
        return Response({"detail": "decision must be approved, declined or clear"},
                        status=status.HTTP_400_BAD_REQUEST)
    for key in keys:
        UpdateDecision.objects.update_or_create(
            kind=kind, key=key, defaults={"decision": decision, "decided_by": request.user})
    return Response({decision: len(keys)})
