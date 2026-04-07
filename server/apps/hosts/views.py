from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.metrics.models import MetricPoint
from apps.tasks.models import Task
from vigil.signing import get_public_key_b64, sign_task

from .authentication import authenticate_agent
from .models import Host
from .serializers import HostSerializer

_MAX_TOKEN_LEN = 255
_MAX_HOSTNAME_LEN = 255


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    """Register a new agent. Creates a pending Host awaiting admin approval."""
    token = request.data.get("agent_token", "").strip()
    hostname = request.data.get("hostname", "").strip()

    if not token or not hostname:
        return Response(
            {"error": "agent_token and hostname are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(token) > _MAX_TOKEN_LEN or len(hostname) > _MAX_HOSTNAME_LEN:
        return Response(
            {"error": "Field value too long"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Idempotent: if the token already exists, return current status
    existing = Host.objects.filter(agent_token=token).first()
    if existing:
        return Response(
            {"id": str(existing.id), "status": existing.status},
            status=status.HTTP_200_OK,
        )

    host = Host.objects.create(
        hostname=hostname,
        os=request.data.get("os", "")[:100],
        kernel=request.data.get("kernel", "")[:100],
        ip_address=request.META.get("REMOTE_ADDR"),
        agent_token=token,
        status=Host.Status.PENDING,
    )
    return Response(
        {"id": str(host.id), "status": host.status},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def checkin(request):
    host, err = authenticate_agent(request)
    if err:
        return err

    if host.status == Host.Status.REJECTED:
        return Response(
            {"error": "Host enrollment has been rejected"},
            status=status.HTTP_403_FORBIDDEN,
        )

    data = request.data

    # Update host metadata from the payload
    host.last_checkin = now()
    if ip := (data.get("ip_address") or request.META.get("REMOTE_ADDR")):
        host.ip_address = ip
    for field in ("hostname", "os", "kernel"):
        if val := data.get(field):
            setattr(host, field, val)

    # A previously offline host that successfully checks in is back online
    if host.status == Host.Status.OFFLINE:
        host.status = Host.Status.ONLINE

    host.save()

    # Pending hosts must wait for admin approval before receiving tasks
    if host.status == Host.Status.PENDING:
        return Response({"status": "pending", "tasks": []})

    # Ingest metrics
    raw_metrics = data.get("metrics", [])
    if raw_metrics:
        points = []
        for m in raw_metrics:
            try:
                time_val = parse_datetime(m["time"]) if m.get("time") else now()
                points.append(
                    MetricPoint(
                        host=host,
                        time=time_val or now(),
                        category=m["category"],
                        metric=m["metric"],
                        value=float(m["value"]),
                        labels=m.get("labels", {}),
                    )
                )
            except (KeyError, ValueError, TypeError):
                pass  # Skip malformed metric entries silently
        if points:
            MetricPoint.objects.bulk_create(points)

    # Sign and hand off any pending tasks
    pending = list(Task.objects.filter(host=host, state=Task.State.PENDING))
    tasks_payload = []
    if pending:
        for task in pending:
            if not task.signature:
                task.signature = sign_task(task)
                task.save(update_fields=["signature"])
            tasks_payload.append(
                {
                    "id": str(task.id),
                    "host_id": str(task.host_id),
                    "action": task.action,
                    "params": task.params,
                    "nonce": task.nonce,
                    "signature": task.signature,
                    "ttl_seconds": task.ttl_seconds,
                    "created_at": task.created_at.isoformat(),
                }
            )
        Task.objects.filter(id__in=[t.id for t in pending]).update(
            state=Task.State.DISPATCHED, dispatched_at=now()
        )

    return Response(
        {
            "status": "ok",
            "public_key": get_public_key_b64(),
            "tasks": tasks_payload,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_list(request):
    hosts = Host.objects.exclude(status=Host.Status.REJECTED)
    return Response(HostSerializer(hosts, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_detail(request, host_id):
    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(HostSerializer(host).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_approve(request, host_id):
    """Approve a pending host enrollment."""
    try:
        host = Host.objects.get(pk=host_id, status=Host.Status.PENDING)
    except Host.DoesNotExist:
        return Response(
            {"error": "Host not found or not pending"},
            status=status.HTTP_404_NOT_FOUND,
        )
    host.status = Host.Status.ONLINE
    host.save()
    return Response(HostSerializer(host).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_poll(request, host_id):
    """Request an immediate check-in.

    Since agents are outbound-only, the server cannot push to them. This
    returns the current host status; the agent will pick up any queued tasks
    on its next scheduled check-in.
    """
    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(HostSerializer(host).data)