from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Alert
from .serializers import AlertSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def alert_list(request):
    state = request.query_params.get("state", Alert.State.FIRING)
    alerts = Alert.objects.filter(state=state).select_related("host", "rule")
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
    alert.state = Alert.State.ACKNOWLEDGED
    alert.acknowledged_at = now()
    alert.save()
    return Response(AlertSerializer(alert).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def alert_silence(request, alert_id):
    try:
        alert = Alert.objects.get(pk=alert_id)
    except Alert.DoesNotExist:
        return Response({"error": "Alert not found"}, status=404)
    if alert.state == Alert.State.RESOLVED:
        return Response({"error": "Cannot silence a resolved alert"}, status=400)
    alert.state = Alert.State.ACKNOWLEDGED
    if not alert.acknowledged_at:
        alert.acknowledged_at = now()
    alert.save()
    return Response(AlertSerializer(alert).data)