from datetime import timedelta
from uuid import UUID

from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Alert
from .serializers import AlertSerializer


def _parse_ack_duration(request):
    """Read an optional ``duration_seconds`` from the request body.

    Returns (acknowledged_until, error_response). Absent/empty means a
    permanent acknowledgement (None); anything else must be a positive
    integer number of seconds.
    """
    raw = request.data.get("duration_seconds")
    if raw is None or raw == "":
        return None, None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None, Response({"error": "duration_seconds must be an integer"}, status=400)
    if seconds <= 0:
        return None, Response({"error": "duration_seconds must be positive"}, status=400)
    return now() + timedelta(seconds=seconds), None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_list(request):
    state = request.query_params.get("state", Alert.State.FIRING)
    alerts = Alert.objects.filter(state=state).select_related("host", "rule")
    # Optional cap — the resolved tab only ever shows recent history
    try:
        limit = int(request.query_params.get("limit", 0))
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        alerts = alerts[: min(limit, 200)]
    return Response(AlertSerializer(alerts, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_acknowledge(request, alert_id):
    try:
        alert = Alert.objects.get(pk=alert_id)
    except Alert.DoesNotExist:
        return Response({"error": "Alert not found"}, status=404)
    if alert.state != Alert.State.FIRING:
        return Response({"error": f"Alert is already '{alert.state}'"}, status=400)
    until, err = _parse_ack_duration(request)
    if err is not None:
        return err
    alert.state = Alert.State.ACKNOWLEDGED
    alert.acknowledged_at = now()
    alert.acknowledged_until = until
    alert.save(update_fields=["state", "acknowledged_at", "acknowledged_until"])
    return Response(AlertSerializer(alert).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_unacknowledge(request, alert_id):
    """Put an acknowledged alert back into FIRING immediately."""
    try:
        alert = Alert.objects.get(pk=alert_id)
    except Alert.DoesNotExist:
        return Response({"error": "Alert not found"}, status=404)
    if alert.state != Alert.State.ACKNOWLEDGED:
        return Response({"error": f"Alert is '{alert.state}', not acknowledged"}, status=400)
    alert.state = Alert.State.FIRING
    alert.acknowledged_at = None
    alert.acknowledged_until = None
    alert.save(update_fields=["state", "acknowledged_at", "acknowledged_until"])
    return Response(AlertSerializer(alert).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_bulk(request):
    """Acknowledge or un-acknowledge a batch of alerts in one request.

    Body: {"ids": [...], "action": "acknowledge"|"unacknowledge",
    "duration_seconds": <optional, acknowledge only>}. Alerts that aren't in
    the right state for the action are counted as skipped, not errors — the
    console lets a selection span tabs.
    """
    ids = request.data.get("ids")
    action = request.data.get("action")
    if not isinstance(ids, list) or not ids:
        return Response({"error": "ids must be a non-empty list"}, status=400)
    if len(ids) > 200:
        return Response({"error": "Too many ids (max 200)"}, status=400)
    if action not in ("acknowledge", "unacknowledge"):
        return Response({"error": "action must be 'acknowledge' or 'unacknowledge'"}, status=400)

    until = None
    if action == "acknowledge":
        until, err = _parse_ack_duration(request)
        if err is not None:
            return err

    valid_ids = []
    for raw in ids:
        try:
            valid_ids.append(UUID(str(raw)))
        except (TypeError, ValueError):
            pass

    current = now()
    updated = 0
    skipped = len(ids) - len(valid_ids)
    alerts = Alert.objects.filter(pk__in=valid_ids)
    skipped += len(valid_ids) - alerts.count()
    for alert in alerts:
        if action == "acknowledge" and alert.state == Alert.State.FIRING:
            alert.state = Alert.State.ACKNOWLEDGED
            alert.acknowledged_at = current
            alert.acknowledged_until = until
        elif action == "unacknowledge" and alert.state == Alert.State.ACKNOWLEDGED:
            alert.state = Alert.State.FIRING
            alert.acknowledged_at = None
            alert.acknowledged_until = None
        else:
            skipped += 1
            continue
        alert.save(update_fields=["state", "acknowledged_at", "acknowledged_until"])
        updated += 1

    return Response({"updated": updated, "skipped": skipped})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_silence(request, alert_id):
    try:
        alert = Alert.objects.get(pk=alert_id)
    except Alert.DoesNotExist:
        return Response({"error": "Alert not found"}, status=404)
    if alert.state == Alert.State.RESOLVED:
        return Response({"error": "Cannot silence a resolved alert"}, status=400)
    until, err = _parse_ack_duration(request)
    if err is not None:
        return err
    alert.state = Alert.State.ACKNOWLEDGED
    if not alert.acknowledged_at:
        alert.acknowledged_at = now()
    alert.acknowledged_until = until
    alert.save(update_fields=["state", "acknowledged_at", "acknowledged_until"])
    return Response(AlertSerializer(alert).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def rule_list(request):
    """Alert rules, for the automation 'specific rule' selector."""
    from .models import AlertRule

    rows = AlertRule.objects.order_by("name")
    return Response([
        {"id": str(r.id), "name": r.name, "category": r.category,
         "severity": r.severity, "metric": r.metric}
        for r in rows
    ])
