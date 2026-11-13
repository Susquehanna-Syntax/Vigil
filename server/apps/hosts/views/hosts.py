"""apps.hosts.views: hosts views (split from the old single module, unchanged)."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from ..api_schema import (
    HostsAdConfigSavedSerializer,
    HostsAdConfigSerializer,
    HostsAdConfigWriteSerializer,
    HostsAdSyncInlineSerializer,
    HostsAdSyncQueuedSerializer,
    HostsDeletedSerializer,
    HostsDeviceSerializer,
    HostsDeviceWriteSerializer,
    HostsErrorSerializer,
    HostsHostSerializer,
    HostsTagsWriteSerializer,
    HostsTaskQueuedSerializer,
    HostsTotpSerializer,
    HostsValidationErrorSerializer,
)
from ._common import *  # noqa: F401,F403


@extend_schema(methods=["GET"], responses=HostsHostSerializer(many=True))
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("hosts")
def host_list(request):
    hosts = list(scoping.filter_by_site(
        Host.objects.exclude(status=Host.Status.REJECTED), request.user))
    ctx = {"host_sites": scoping.sites_for_hosts([h.id for h in hosts])}
    return Response(HostSerializer(hosts, many=True, context=ctx).data)


@extend_schema(methods=["GET"], responses=HostsHostSerializer)
@extend_schema(methods=["DELETE"], responses={200: HostsDeletedSerializer,
                                              403: HostsErrorSerializer,
                                              404: HostsErrorSerializer})
@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
def host_detail(request, host_id):
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    # Out of scope reads as absent: a 403 would confirm the host exists in a
    # site this user cannot see.
    if not scoping.host_in_scope(request.user, host):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        # Destructive removal of a host and all its history is admin-only;
        # viewing stays open to any authenticated session.
        if not IsAdmin().has_permission(request, None):
            return Response({"error": "Administrator access required"}, status=status.HTTP_403_FORBIDDEN)
        hostname = host.hostname
        host.delete()
        return Response({"deleted": hostname}, status=status.HTTP_200_OK)
    return Response(HostSerializer(
        host, context={"host_sites": scoping.sites_for_hosts([host.id])}).data)


@extend_schema(methods=["GET"], responses=HostsDeviceSerializer(many=True))
@extend_schema(methods=["POST"], request=HostsDeviceWriteSerializer,
               responses={201: HostsDeviceSerializer,
                          400: HostsValidationErrorSerializer,
                          403: HostsErrorSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def unmanaged_devices(request):
    """List every unmanaged device, or add one (manual network inventory)."""
    if request.method == "GET":
        qs = UnmanagedDevice.objects.all()
        return Response(UnmanagedDeviceSerializer(qs, many=True).data)

    # Editing and deleting a device are admin-only; adding one is the same kind
    # of inventory write (SEC-1).
    from apps.accounts.permissions import IsAdmin
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Administrator access required."}, status=status.HTTP_403_FORBIDDEN)
    site_id, denied = scoping.site_for_write(request.user, request.data.get("site_id"))
    if denied:
        return denied
    serializer = UnmanagedDeviceSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    serializer.save(site_id=site_id)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(methods=["GET"], responses={200: HostsDeviceSerializer,
                                           404: HostsErrorSerializer})
@extend_schema(methods=["PATCH"], request=HostsDeviceWriteSerializer,
               responses={200: HostsDeviceSerializer,
                          400: HostsValidationErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@extend_schema(methods=["DELETE"], responses={204: None,
                                              403: HostsErrorSerializer,
                                              404: HostsErrorSerializer})
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def unmanaged_device_detail(request, device_id):
    """Read, update, or delete one unmanaged device."""
    try:
        device = UnmanagedDevice.objects.get(pk=device_id)
    except UnmanagedDevice.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(UnmanagedDeviceSerializer(device).data)

    # Same rule as the list endpoint above (SEC-1): reads are open to every
    # signed-in user, writes are admin-only.
    from apps.accounts.permissions import IsAdmin
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Administrator access required."}, status=status.HTTP_403_FORBIDDEN)
    if denied := scoping.write_denied(request.user, device):
        return denied

    if request.method == "DELETE":
        device.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    extra = {}
    if "site_id" in request.data:
        extra["site_id"], denied = scoping.site_for_write(request.user, request.data["site_id"])
        if denied:
            return denied
    serializer = UnmanagedDeviceSerializer(device, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    serializer.save(**extra)
    return Response(serializer.data)


@extend_schema(methods=["GET"], responses=HostsAdConfigSerializer)
@extend_schema(methods=["POST"], request=HostsAdConfigWriteSerializer,
               responses={200: HostsAdConfigSavedSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAdmin, SettingsPage("directory")])
def ad_config(request):
    """Read or update the AD import settings. Admin-only — the config holds
    directory bind credentials and controls what gets imported.

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


@extend_schema(request=None,
               responses={200: HostsAdSyncInlineSerializer,
                          201: HostsAdSyncQueuedSerializer})
@api_view(["POST"])
@permission_classes([IsAdmin, SettingsPage("directory")])
def ad_sync_now(request):
    """Trigger an AD import synchronously (small fleets) or via Celery."""
    from ..tasks import import_ad_computers

    # Run inline if Celery isn't available so the admin UI still works in
    # single-process dev. Production should use the queued path.
    try:
        result = import_ad_computers.delay()
        return Response({"queued": True, "task_id": str(result.id)})
    except Exception:
        result = import_ad_computers()
        return Response({"queued": False, "result": result})


@extend_schema(request=HostsTagsWriteSerializer,
               responses={200: HostsHostSerializer,
                          400: HostsErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def host_tags(request, host_id):
    """Replace the tag set on a host (operator-driven from the console)."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    # Tags decide which wave patches this machine, which playbooks target it and
    # which automations fire on it — so retagging is a way to get work run on a
    # box, not a cosmetic edit.
    if not can(request.user, scoping.scope_of(host), "hosts", "edit"):
        return Response({"error": "Not permitted"}, status=status.HTTP_403_FORBIDDEN)

    raw = request.data.get("tags")
    if not isinstance(raw, list):
        return Response({"error": "tags must be a list"}, status=status.HTTP_400_BAD_REQUEST)
    from ..models import Tag
    wanted = _normalize_tags(raw)
    keys = {Tag.canonical_key(t) for t in wanted if isinstance(t, str)}
    existing = {t.key: t for t in Tag.objects.filter(key__in=keys)}
    # S1 04h: a host carries global tags and its own site's, never another
    # site's. The message names the tag (the caller typed it) but not the
    # site that owns it.
    unfit = sorted(t.name for t in existing.values() if not _tag_fits_host(t, host))
    if unfit:
        return Response({"error": f"Not available to this host: {', '.join(unfit)}"},
                        status=status.HTTP_400_BAD_REQUEST)
    # A tag first typed here by a site admin belongs to the host's site; an
    # instance admin's stays global, as every tag was before. The new rows
    # are made here, with their site, before the save's tag sync looks them
    # up: claiming them afterwards could claim a global tag someone else made
    # in between (security review of 5c04a28).
    host_site = scoping.site_id_of(host)
    claim = host_site if host_site is not None and not scoping.is_instance_admin(request.user) else None
    with transaction.atomic():
        for name in wanted:
            if not isinstance(name, str) or Tag.canonical_key(name) in existing or not name.strip():
                continue
            tag, created = Tag.objects.get_or_create(
                key=Tag.canonical_key(name), defaults={"name": name, "site_id": claim})
            if not created and not _tag_fits_host(tag, host):
                transaction.set_rollback(True)
                return Response({"error": f"Not available to this host: {tag.name}"},
                                status=status.HTTP_400_BAD_REQUEST)
        host.tags = wanted
        host.save(update_fields=["tags", "updated_at"])
    return Response(HostSerializer(host).data)


@extend_schema(request=HostsTotpSerializer,
               responses={201: HostsTaskQueuedSerializer,
                          409: HostsTaskQueuedSerializer,
                          400: HostsErrorSerializer,
                          401: HostsErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer,
                          503: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_update_agent(request, host_id):
    """Force an agent self-update on one host. 2FA-gated.

    Queues a single ``update_agent`` task carrying the verified per-
    platform SHA-256 the agent checks before swapping its binary (the
    download is TLS-only, so the signed digest is the real proof). The
    task is signed and handed over on the host's next check-in.

    The agent's mode stays authoritative: a monitor-mode host is refused
    here, and a managed-mode agent still has to allowlist ``update_agent``
    or it will reject the task on arrival — by design, the server can't
    force an update past the agent's local policy.
    """
    from apps.accounts.totp import require_totp_confirmation
    from apps.agent_dist.views import all_binary_sha256

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    # TOTP proves who is asking, never that they may. Replacing the agent binary
    # swaps the executable that enforces the agent's own allowlist, so it needs
    # the capability as well as the code.
    if not can(request.user, scoping.scope_of(host), "hosts", "update_agent"):
        return Response({"error": "Not permitted"}, status=status.HTTP_403_FORBIDDEN)

    if host.mode == Host.Mode.MONITOR:
        return Response(
            {"error": "Host is in monitor mode — it does not execute tasks"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    error = require_totp_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=status.HTTP_401_UNAUTHORIZED)

    sha_map = all_binary_sha256()
    if not sha_map:
        return Response(
            {"error": "No agent binaries are available on the server to update to"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Don't stack duplicate update tasks — if one is already queued or in
    # flight for this host, point the caller at it instead.
    existing = Task.objects.filter(
        host=host,
        action="update_agent",
        state__in=[Task.State.PENDING, Task.State.DISPATCHED, Task.State.EXECUTING],
    ).first()
    if existing:
        return Response(
            {"status": "already_queued", "task_id": str(existing.id)},
            status=status.HTTP_409_CONFLICT,
        )

    task = Task.objects.create(
        host=host,
        requested_by=request.user,
        action="update_agent",
        params={"binary_sha256": sha_map},
        risk_level=Task.RiskLevel.STANDARD,
        state=Task.State.PENDING,
        nonce=secrets.token_hex(32),
    )
    return Response({"status": "queued", "task_id": str(task.id)}, status=status.HTTP_201_CREATED)


@extend_schema(request=HostsTotpSerializer,
               responses={200: HostsHostSerializer,
                          401: HostsErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAdmin])
def host_approve(request, host_id):
    """Approve a pending host enrollment.

    Admin-only and 2FA-gated because an approved host immediately becomes
    eligible to receive signed tasks — equivalent in blast-radius to
    dispatching a task.
    """
    from apps.accounts.totp import require_totp_confirmation

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    if host.status != Host.Status.PENDING:
        return Response(
            {"error": "Host not found or not pending"},
            status=status.HTTP_404_NOT_FOUND,
        )

    error = require_totp_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=status.HTTP_401_UNAUTHORIZED)

    candidate = replacement_candidate(host)
    if candidate is not None:
        host = adopt_enrolment(host, candidate)
    else:
        host.status = Host.Status.ONLINE
        host.save()

    if host.machine_id:
        Host.objects.filter(
            machine_id=host.machine_id, status=Host.Status.PENDING
        ).exclude(pk=host.pk).delete()

    # Extension seam: Pro playbooks auto-dispatch on this event; Enterprise
    # audit logs record the approval. No-op in Community. See vigil/hooks.py.
    from vigil.hooks import emit
    emit("host_approved", host=host, approved_by=request.user)

    return Response(HostSerializer(host).data)


@extend_schema(request=None,
               responses={200: HostsHostSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAdmin])
def host_reject(request, host_id):
    """Reject a pending host enrollment. Admin-only, like approval."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    if host.status != Host.Status.PENDING:
        return Response(
            {"error": "Host not found or not pending"},
            status=status.HTTP_404_NOT_FOUND,
        )

    host.status = Host.Status.REJECTED
    host.save()

    from vigil.hooks import emit
    emit("host_rejected", host=host, rejected_by=request.user)

    return Response(HostSerializer(host).data)


@extend_schema(methods=["GET"],
               responses={(200, "application/x-rdp"): OpenApiTypes.BINARY,
                          (400, "application/json"): HostsErrorSerializer,
                          (404, "application/json"): HostsErrorSerializer})
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

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

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
