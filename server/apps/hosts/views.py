import zoneinfo

from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.metrics.models import MetricPoint
from apps.tasks.models import Task
from apps.tasks.spec import schedule_window_active
from vigil.signing import get_public_key_b64, sign_task

from .auto_tags import merge_auto_tags
from .authentication import authenticate_agent
from .crypto import encrypt_secret
from .models import ADConfig, Host, HostInventory
from .serializers import HostInventorySerializer, HostSerializer

_MAX_TOKEN_LEN = 255
_MAX_HOSTNAME_LEN = 255


_MAX_TAGS = 32


def _normalize_tags(raw, *, existing=None):
    """Sanitize incoming tag lists from agents.

    Returns a sorted, deduped list of lowercase tags merged with *existing*
    (server-side tags always win on conflict — operators can't lose tags
    they set in the console). Drops anything that isn't a non-empty string,
    truncates each to 40 chars, and caps the total at _MAX_TAGS.
    """
    if not isinstance(raw, list):
        return list(existing or [])
    out: list[str] = list(existing or [])
    seen = {t for t in out}
    for entry in raw:
        if not isinstance(entry, str):
            continue
        t = entry.strip().lower()[:40]
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= _MAX_TAGS:
            break
    return sorted(out)


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

    seed_tags = _normalize_tags(request.data.get("tags"))

    host = Host.objects.create(
        hostname=hostname,
        os=request.data.get("os", "")[:100],
        kernel=request.data.get("kernel", "")[:100],
        ip_address=request.META.get("REMOTE_ADDR"),
        agent_token=token,
        status=Host.Status.PENDING,
        tags=seed_tags,
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

    # Merge tags advertised by the agent.yml — server-side tags always win.
    if "tags" in data:
        host.tags = _normalize_tags(data.get("tags"), existing=host.tags)

    # A previously offline host that successfully checks in is back online
    if host.status == Host.Status.OFFLINE:
        host.status = Host.Status.ONLINE
        from apps.alerts.models import Alert as _Alert
        _Alert.objects.filter(
            host=host,
            rule=None,
            state=_Alert.State.FIRING,
            message__startswith="Host offline:",
        ).update(state=_Alert.State.RESOLVED, resolved_at=now())

    # Auto-tag based on intrinsic facts (OS family, mode). Runs after the
    # agent.yml merge so derived tags appear alongside operator-set ones.
    host.tags = merge_auto_tags(host)

    host.save()

    # Inventory upsert — agent sends the snapshot on a slower cadence than
    # metrics. Custom columns (populated by collector tasks) are preserved.
    inv_payload = data.get("inventory")
    if isinstance(inv_payload, dict):
        inv, _ = HostInventory.objects.get_or_create(host=host)
        inv.mac_addresses = inv_payload.get("mac_addresses") or {}
        try:
            inv.ram_total_bytes = int(inv_payload.get("ram_total_bytes") or 0) or None
        except (TypeError, ValueError):
            inv.ram_total_bytes = None
        inv.cpu_model = (inv_payload.get("cpu_model") or "")[:255]
        try:
            inv.cpu_cores = int(inv_payload.get("cpu_cores") or 0) or None
        except (TypeError, ValueError):
            inv.cpu_cores = None
        inv.service_tag = (inv_payload.get("service_tag") or "")[:120]
        inv.manufacturer = (inv_payload.get("manufacturer") or "")[:120]
        inv.model_name = (inv_payload.get("model") or inv_payload.get("model_name") or "")[:160]
        disks = inv_payload.get("disks") or []
        inv.disks = disks if isinstance(disks, list) else []
        inv.save()

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

    # Sign and hand off any pending tasks. Two gates apply before dispatch:
    #   1. ``not_before`` — used to space out retries by retry_delay_seconds
    #   2. ``schedule.window`` — agents only receive tasks during the
    #      configured maintenance window. Tasks outside the window remain
    #      PENDING and are picked up at the next eligible checkin.
    from django.conf import settings as _settings
    current = now()
    _tz_name = getattr(_settings, "VIGIL_TIMEZONE", "UTC")
    try:
        _tz = zoneinfo.ZoneInfo(_tz_name)
        _local = current.astimezone(_tz)
    except Exception:
        _local = current
    eligible: list[Task] = []
    candidates = list(Task.objects.filter(host=host, state=Task.State.PENDING))
    if candidates:
        weekday = _local.weekday()
        hour = _local.hour
        minute = _local.minute
        for task in candidates:
            if task.not_before and task.not_before > current:
                continue
            if not schedule_window_active(task.schedule, weekday=weekday, hour=hour, minute=minute):
                continue
            eligible.append(task)

    tasks_payload = []
    if eligible:
        for task in eligible:
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
        Task.objects.filter(id__in=[t.id for t in eligible]).update(
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tag_index(request):
    """Return every tag in use across the fleet with host counts.

    Powers the deploy modal's tag picker and the tag-management UI. Counts
    are computed in Python because tags is a JSONField (no native list
    aggregation across DB backends).
    """
    counts: dict[str, int] = {}
    for tag_list in Host.objects.exclude(status=Host.Status.REJECTED).values_list("tags", flat=True):
        if not tag_list:
            continue
        for t in tag_list:
            if isinstance(t, str):
                counts[t] = counts.get(t, 0) + 1
    out = [{"tag": k, "host_count": v} for k, v in sorted(counts.items())]
    return Response(out)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def inventory_list(request):
    """Inventory rows for every non-rejected host.

    Hosts with no inventory yet are still returned (with empty fields) so
    the UI can show "no agent data yet" alongside actual entries.
    """
    hosts = (
        Host.objects.exclude(status=Host.Status.REJECTED)
        .select_related("inventory")
        .order_by("hostname")
    )
    rows = []
    seen_columns: set[str] = set()
    for host in hosts:
        inv = getattr(host, "inventory", None)
        if inv is None:
            inv = HostInventory(host=host)  # in-memory placeholder, not saved
        rows.append(HostInventorySerializer(inv).data)
        custom = inv.custom_columns or {}
        seen_columns.update(custom.keys())
    return Response({"rows": rows, "custom_columns": sorted(seen_columns)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def inventory_detail(request, host_id):
    try:
        host = Host.objects.select_related("inventory").get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    inv = getattr(host, "inventory", None) or HostInventory(host=host)
    return Response(HostInventorySerializer(inv).data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def ad_config(request):
    """Read or update the AD import settings.

    GET returns the current settings (password redacted). POST upserts the
    config; the password is encrypted at rest and only visible to the
    Celery worker performing the import.
    """
    config = ADConfig.objects.order_by("-updated_at").first()

    if request.method == "GET":
        if config is None:
            return Response({
                "ldap_url": "", "bind_dn": "", "base_dn": "",
                "computer_ou": "", "enabled": False,
                "last_sync": None, "last_sync_status": "",
                "has_password": False,
            })
        return Response({
            "ldap_url": config.ldap_url,
            "bind_dn": config.bind_dn,
            "base_dn": config.base_dn,
            "computer_ou": config.computer_ou,
            "enabled": config.enabled,
            "last_sync": config.last_sync,
            "last_sync_status": config.last_sync_status,
            "has_password": bool(config.bind_password_encrypted),
        })

    config = config or ADConfig()
    config.ldap_url = (request.data.get("ldap_url") or "")[:512]
    config.bind_dn = (request.data.get("bind_dn") or "")[:512]
    config.base_dn = (request.data.get("base_dn") or "")[:512]
    config.computer_ou = (request.data.get("computer_ou") or "")[:512]
    config.enabled = bool(request.data.get("enabled", False))

    new_password = request.data.get("bind_password")
    if isinstance(new_password, str) and new_password:
        config.bind_password_encrypted = encrypt_secret(new_password)

    config.save()
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ad_sync_now(request):
    """Trigger an AD import synchronously (small fleets) or via Celery."""
    from .tasks import import_ad_computers

    # Run inline if Celery isn't available so the admin UI still works in
    # single-process dev. Production should use the queued path.
    try:
        result = import_ad_computers.delay()
        return Response({"queued": True, "task_id": str(result.id)})
    except Exception:
        result = import_ad_computers()
        return Response({"queued": False, "result": result})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def host_tags(request, host_id):
    """Replace the tag set on a host (operator-driven from the console)."""
    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    raw = request.data.get("tags")
    if not isinstance(raw, list):
        return Response({"error": "tags must be a list"}, status=status.HTTP_400_BAD_REQUEST)
    host.tags = _normalize_tags(raw)
    host.save(update_fields=["tags", "updated_at"])
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
def host_reject(request, host_id):
    """Reject a pending host enrollment."""
    try:
        host = Host.objects.get(pk=host_id, status=Host.Status.PENDING)
    except Host.DoesNotExist:
        return Response(
            {"error": "Host not found or not pending"},
            status=status.HTTP_404_NOT_FOUND,
        )
    host.status = Host.Status.REJECTED
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_rdp(request, host_id):
    """Generate a Windows .rdp file pointing at this host.

    Browsers can't invoke mstsc directly, so we serve a downloadable .rdp
    file. On Windows the OS opens it in mstsc; on macOS in Microsoft Remote
    Desktop; on Linux in Remmina/FreeRDP. The user supplies their own
    credentials at connection time — we never store or transmit them.
    """
    from django.http import HttpResponse

    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    target = host.ip_address or host.hostname
    if not target:
        return Response({"error": "Host has no address"}, status=status.HTTP_400_BAD_REQUEST)

    port = request.GET.get("port", "3389")
    try:
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        return Response({"error": "Invalid port"}, status=status.HTTP_400_BAD_REQUEST)

    # Standard .rdp file format. CRLF line endings are required by mstsc.
    lines = [
        f"full address:s:{target}:{port_int}",
        "prompt for credentials:i:1",
        "administrative session:i:0",
        "screen mode id:i:2",
        "use multimon:i:0",
        "audiomode:i:0",
        "redirectclipboard:i:1",
        "redirectprinters:i:0",
        "redirectsmartcards:i:0",
        "authentication level:i:2",
        f"alternate full address:s:{target}",
    ]
    rdp_body = "\r\n".join(lines) + "\r\n"

    # Sanitize hostname for a safe filename
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host.hostname)[:64]
    filename = f"{safe or 'host'}.rdp"

    response = HttpResponse(rdp_body, content_type="application/x-rdp")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response