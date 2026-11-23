"""apps.hosts.views: hosts views (split from the old single module, unchanged)."""
import itertools
import json
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from ..api_schema import (
    HostsAdConfigSavedSerializer,
    HostsAdConfigSerializer,
    HostsAdConfigWriteSerializer,
    HostsAdSyncInlineSerializer,
    HostsAdSyncQueuedSerializer,
    HostsAgentVersionsSerializer,
    HostsDeletedSerializer,
    HostsDeviceCheckInterfaceSeriesSerializer,
    HostsDeviceCheckInterfaceSerializer,
    HostsDeviceCheckResultSerializer,
    HostsDeviceCheckSerializer,
    HostsDeviceCheckWriteSerializer,
    HostsDeviceSerializer,
    HostsDeviceWriteSerializer,
    HostsErrorSerializer,
    HostsHostSerializer,
    HostsTagsWriteSerializer,
    HostsTaskQueuedSerializer,
    HostsTotpSerializer,
    HostsValidationErrorSerializer,
)
from apps.alerts.device_alerts import resolve_for_check
from apps.alerts.tasks import _is_older, _version_key
from ..crypto import encrypt_secret
from ..device_checks import (DEFAULT_SSH_INTERVAL, MIN_SSH_INTERVAL, device_states, ssh_private_key,
                             stored_v3_passwords, validate_interval, validate_params)
from ..models import DeviceCheck, Host
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


def _recent_update_at(raw, since):
    """The parsed ``at`` of an agent_update report when it is inside the window,
    else None. A report with no readable time is ignored, not counted."""
    if not isinstance(raw, str):
        return None
    try:
        at = parse_datetime(raw)
    except ValueError:
        return None
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt_timezone.utc)
    return at if at >= since else None


def _newest_first(version):
    """Sort key: known versions newest first by number, unknown or unparseable last."""
    key = _version_key(version)
    if key is None:
        return (1, ())
    padded = key + (0,) * (8 - len(key))
    return (0, tuple(-part for part in padded))


@extend_schema(methods=["GET"], responses=HostsAgentVersionsSerializer)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("hosts")
def agent_versions(request):
    """Which agent versions the approved fleet runs, against the version this
    server ships, and how self-updates from the last 30 days ended."""
    expected = settings.VIGIL_AGENT_VERSION or ""
    since = now() - timedelta(days=30)
    hosts = scoping.filter_by_site(
        Host.objects.filter(status__in=[Host.Status.ONLINE, Host.Status.OFFLINE]),
        request.user,
    ).values("id", "hostname", "agent_version", "agent_update")

    counts = {}
    updates = {"confirmed": 0, "rolled_back": 0, "not_applied": 0}
    rolled_back = []
    for row in hosts:
        version = row["agent_version"] or ""
        counts[version] = counts.get(version, 0) + 1
        update = row["agent_update"]
        if not isinstance(update, dict):
            continue
        at = _recent_update_at(update.get("at"), since)
        if at is None:
            continue
        state = update.get("state")
        if state in updates:
            updates[state] += 1
        if state == "rolled_back":
            rolled_back.append((at, row, update))

    versions = []
    for version in sorted(counts, key=_newest_first):
        versions.append({
            "version": version,
            "hosts": counts[version],
            "current": bool(expected) and version == expected,
            "outdated": bool(expected) and _is_older(version, expected),
        })

    rolled_back.sort(key=lambda item: item[0], reverse=True)
    rolled_back_rows = [
        {
            "host_id": str(row["id"]),
            "hostname": row["hostname"],
            "from_version": update.get("from_version") or "",
            "to_version": update.get("to_version") or "",
            "reason": update.get("reason") or "",
            "at": update.get("at"),
        }
        for _, row, update in rolled_back[:50]
    ]
    return Response({
        "expected": expected,
        "total": sum(counts.values()),
        "versions": versions,
        "outdated_hosts": sum(v["hosts"] for v in versions if v["outdated"]),
        "updates": updates,
        "rolled_back": rolled_back_rows,
    })


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
        devices = list(UnmanagedDevice.objects.all())
        data = UnmanagedDeviceSerializer(devices, many=True).data
        states = device_states(devices)
        is_admin = IsAdmin().has_permission(request, None)
        can_edit = {d.id: _device_can_edit(request, d, is_admin) for d in devices}
        for row, device in zip(data, devices):
            # check_state is the dot on the row; can_edit_checks says whether
            # to offer the write controls at all (see _device_can_edit).
            row["check_state"] = states.get(device.id)
            row["can_edit_checks"] = can_edit[device.id]
        return Response(data)

    # Editing and deleting a device are admin-only; adding one is the same kind
    # of inventory write (SEC-1).
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


def _community_value(raw):
    """The community of an SNMP check: 1 to 64 printable ASCII characters."""
    if not isinstance(raw, str) or not (1 <= len(raw) <= 64) \
            or not all(32 <= ord(c) <= 126 for c in raw):
        raise ValueError("community must be 1 to 64 printable characters")
    return encrypt_secret(raw)


def _v3_password(raw, name: str) -> str:
    """An SNMPv3 password: 8 to 64 printable ASCII characters."""
    if not isinstance(raw, str) or not (8 <= len(raw) <= 64) \
            or not all(32 <= ord(c) <= 126 for c in raw):
        raise ValueError(f"{name} must be 8 to 64 printable characters")
    return raw


def _v3_secret(auth_password: str, priv_password: str | None) -> bytes:
    """The secret column of an SNMPv3 check: both passwords, JSON, encrypted."""
    return encrypt_secret(json.dumps({"auth": auth_password, "priv": priv_password}))


def _check_out(check) -> dict:
    latest = check.results.order_by("-time").first()
    params = check.params if isinstance(check.params, dict) else {}
    v3 = params.get("version") == "3" and bool(check.secret)
    passwords = stored_v3_passwords(check) if v3 else None
    return {
        "id": str(check.id),
        "kind": check.kind,
        "params": check.params,
        # A v3 secret holds no community, so has_community says nothing about it;
        # the two v3 flags answer for its passwords instead. Never a password.
        "has_community": bool(check.secret) and not v3,
        "has_auth_password": bool(passwords),
        "has_priv_password": bool(passwords and passwords["priv"]),
        # An SSH check's private key: whether one is stored, never the key.
        "has_key": check.kind == DeviceCheck.Kind.SSH and bool(ssh_private_key(check)),
        "interval_seconds": check.interval_seconds,
        "enabled": check.enabled,
        "poller": str(check.poller_id) if check.poller_id else None,
        "created_at": check.created_at,
        "latest": ({"ok": latest.ok, "latency_ms": latest.latency_ms,
                    "detail": latest.detail, "time": latest.time,
                    "extra": latest.extra} if latest else None),
    }


def _device_can_edit(request, device, is_admin) -> bool:
    """Whether this user may add, change or delete a device's checks.

    The check writes refuse exactly this combination (admin role and a device
    inside the user's write scope), so the list answers with it and the page
    offers only what the server will accept. *is_admin* is asked once per list
    by the caller: the role never varies by device, but whether the device is
    in this user's site does.
    """
    return bool(is_admin and not scoping.write_denied(request.user, device))


def _check_fields(device, data, current=None) -> dict:
    """Validated check fields from a request body, merged over ``current`` on PATCH.

    Raises ValueError with the one sentence an API answer carries.
    """
    fields = {}
    if current is None or "kind" in data or "params" in data:
        kind = data.get("kind", current.kind if current else None)
        params = data.get("params", current.params if current else {})
        fields["kind"] = kind
        fields["params"] = validate_params(kind, params)
    if "interval_seconds" in data or current is None:
        fields["interval_seconds"] = validate_interval(data.get("interval_seconds", 60))
    if "enabled" in data or current is None:
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        fields["enabled"] = enabled
    if "poller" in data or current is None:
        raw = data.get("poller")
        if raw in (None, ""):
            fields["poller"] = None
        else:
            try:
                poller = Host.objects.get(pk=raw)
            except (Host.DoesNotExist, ValueError, DjangoValidationError):
                poller = None
            if poller is None or not poller.agent_poller or (
                    device.site_id is not None and scoping.site_id_of(poller) != device.site_id):
                raise ValueError("poller must be a poller host of this device's site")
            fields["poller"] = poller
    # The secrets live on their own encrypted column, never in params. For v2c
    # that column holds the community: an SNMP check needs one when it is
    # created and keeps the stored one when a PATCH leaves it out. For v3 it
    # holds both passwords as JSON. Any other kind has none, so a check changed
    # away from SNMP forgets them and a secret in that body is ignored.
    # encrypt_secret("") is b"", which is the field's default.
    kind = fields.get("kind", current.kind if current else None)
    if kind == DeviceCheck.Kind.SSH:
        _ssh_fields(fields, data, current)
    elif kind == DeviceCheck.Kind.SNMP:
        params = fields.get("params", current.params if current else {})
        if params.get("version") == "3":
            fields["secret"] = _v3_fields(data, params, current)
        else:
            fields["secret"] = _v2c_secret(data, current)
    elif kind is not None:
        fields["secret"] = encrypt_secret("")
    return fields


def _new_ssh_key() -> tuple[bytes, str]:
    """A fresh Ed25519 key pair: (encrypted OpenSSH private key, OpenSSH public key line)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH).decode()
    return encrypt_secret(private), f"{public} vigil-config-backup"


def _ssh_fields(fields, data, current) -> None:
    """The SSH-only parts of a check write, in place on *fields* (S9 08).

    - Config backup is a Business feature: a check becomes SSH only under a
      license that grants it. One that already is can still be edited,
      disabled or deleted after the license lapses (it is simply not handed
      to a poller, device_checks._ssh_wire_params).
    - It runs at most every 15 minutes, daily unless the body says otherwise.
    - The server makes the key pair. The private half is the check's secret
      and is never answered; the public half goes in params for an admin to
      put in the device's authorized keys. A PATCH keeps the pair unless it
      sends ``rotate_key: true``.
    - A pinned host key survives an edit that does not mention it, as long as
      the host and port stay the same; ``host_key: ""`` unpins it so the next
      run learns the device's key again.
    """
    from vigil.licensing import has_feature

    was_ssh = current is not None and current.kind == DeviceCheck.Kind.SSH
    if not was_ssh and not has_feature("config_backup"):
        raise ValueError("SSH config backup needs a Business license")
    if "interval_seconds" not in data and not was_ssh:
        fields["interval_seconds"] = DEFAULT_SSH_INTERVAL
    interval = fields.get("interval_seconds", current.interval_seconds if current else None)
    if interval is not None and interval < MIN_SSH_INTERVAL:
        raise ValueError("an SSH check runs at most every 15 minutes (interval_seconds 900 or more)")

    old = dict(current.params) if was_ssh and isinstance(current.params, dict) else {}
    params = fields.get("params")
    if params is None:
        params = dict(old)
    else:
        sent = data.get("params") if isinstance(data.get("params"), dict) else {}
        same_target = (params.get("host") == old.get("host")
                       and params.get("port", 22) == old.get("port", 22))
        if "host_key" not in sent and old.get("host_key") and same_target:
            params["host_key"] = old["host_key"]
    rotate = data.get("rotate_key", False)
    if not isinstance(rotate, bool):
        raise ValueError("rotate_key must be true or false")
    if was_ssh and not rotate and ssh_private_key(current) and old.get("public_key"):
        fields["secret"] = current.secret
        params["public_key"] = old["public_key"]
    else:
        fields["secret"], params["public_key"] = _new_ssh_key()
    fields["params"] = params


def _v2c_secret(data, current) -> bytes:
    """The community column of a v2c check: the body's, or the stored one."""
    if "community" in data:
        return _community_value(data["community"])
    # A PATCH that leaves the community out keeps the stored one — unless it is
    # a v3 secret, which holds no community to keep.
    if current is not None and current.secret and (current.params or {}).get("version") != "3":
        return current.secret
    raise ValueError("an SNMP check needs a community")


def _v3_fields(data, params, current) -> bytes:
    """The password column of an SNMPv3 check, from the body over the stored one.

    A ``community`` in this body is ignored: v3 has none. Passwords given in the
    body are validated first; one taken back out of the column is not — it was
    checked when it was written, and re-checking it here would refuse a PATCH
    that changes nothing about the secrets. Never in an exception message.
    """
    # Only a check that was already v3 has passwords to keep: a v2c community is
    # never read as one, whatever text it happens to be.
    was_v3 = current is not None and (current.params or {}).get("version") == "3"
    stored = stored_v3_passwords(current) if was_v3 else None
    if "auth_password" in data:
        auth = _v3_password(data["auth_password"], "auth_password")
    elif stored is not None:
        auth = stored["auth"]
    else:
        raise ValueError("an SNMPv3 check needs an auth password")

    if params.get("priv", True):
        if "priv_password" in data:
            priv_password = _v3_password(data["priv_password"], "priv_password")
        elif stored is not None and stored["priv"] is not None:
            priv_password = stored["priv"]
        else:
            raise ValueError("an SNMPv3 check with privacy needs a privacy password")
    else:
        # `priv` false says the check does not encrypt: no privacy password.
        priv_password = None
    return _v3_secret(auth, priv_password)


@extend_schema(methods=["GET"], responses=HostsDeviceCheckSerializer(many=True))
@extend_schema(methods=["POST"], request=HostsDeviceCheckWriteSerializer,
               responses={201: HostsDeviceCheckSerializer,
                          400: HostsValidationErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def device_checks(request, device_id):
    """List a device's checks with each one's latest result, or add a check (admin)."""
    try:
        device = UnmanagedDevice.objects.get(pk=device_id)
    except UnmanagedDevice.DoesNotExist:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response([_check_out(c) for c in device.checks.order_by("created_at", "id")])

    # Same rule as the device writes above (SEC-1): reads open, writes admin-only.
    from apps.accounts.permissions import IsAdmin
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Administrator access required."}, status=status.HTTP_403_FORBIDDEN)
    if denied := scoping.write_denied(request.user, device):
        return denied
    if not isinstance(request.data, dict):
        return Response({"error": "The body must be an object."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        fields = _check_fields(device, request.data)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    check = DeviceCheck.objects.create(device=device, **fields)
    return Response(_check_out(check), status=status.HTTP_201_CREATED)


@extend_schema(methods=["PATCH"], request=HostsDeviceCheckWriteSerializer,
               responses={200: HostsDeviceCheckSerializer,
                          400: HostsValidationErrorSerializer,
                          403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@extend_schema(methods=["DELETE"], responses={204: None,
                                              403: HostsErrorSerializer,
                                              404: HostsErrorSerializer})
@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def device_check_detail(request, device_id, check_id):
    """Change or delete one device check (admin)."""
    try:
        device = UnmanagedDevice.objects.get(pk=device_id)
        check = DeviceCheck.objects.get(pk=check_id, device=device)
    except (UnmanagedDevice.DoesNotExist, DeviceCheck.DoesNotExist):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

    from apps.accounts.permissions import IsAdmin
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Administrator access required."}, status=status.HTTP_403_FORBIDDEN)
    if denied := scoping.write_denied(request.user, device):
        return denied

    if request.method == "DELETE":
        # Resolve first: the alert's device_check FK is SET_NULL, so deleting
        # the check would leave open alerts nothing can resolve (S9 04b).
        resolve_for_check(check)
        check.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if not isinstance(request.data, dict):
        return Response({"error": "The body must be an object."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        fields = _check_fields(device, request.data, current=check)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    for key, value in fields.items():
        setattr(check, key, value)
    check.save()
    if not check.enabled:
        # A stopped check would otherwise leave its alert firing forever with
        # nobody polling to resolve it (S9 04b).
        resolve_for_check(check)
    return Response(_check_out(check))


@extend_schema(methods=["GET"],
               parameters=[OpenApiParameter(
                   "hours", int, required=False,
                   description="How far back to read, 1 to 720 hours. Default 24.")],
               responses={200: HostsDeviceCheckResultSerializer(many=True),
                          400: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def device_check_results(request, device_id, check_id):
    """The stored results of one device check, newest last, at most 2000 rows."""
    try:
        device = UnmanagedDevice.objects.get(pk=device_id)
        check = DeviceCheck.objects.get(pk=check_id, device=device)
    except (UnmanagedDevice.DoesNotExist, DeviceCheck.DoesNotExist):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    try:
        hours = int(request.query_params.get("hours", 24))
    except (TypeError, ValueError):
        hours = 0
    if not 1 <= hours <= 720:
        return Response({"error": "hours must be a whole number from 1 to 720"},
                        status=status.HTTP_400_BAD_REQUEST)
    rows = list(check.results.filter(time__gte=now() - timedelta(hours=hours))
                .order_by("-time")[:2000])
    rows.reverse()
    return Response([{"time": r.time, "ok": r.ok, "latency_ms": r.latency_ms,
                      "detail": r.detail} for r in rows])


MAX_SERIES_POINTS = 2000


def _check_window(request, device_id, check_id):
    """(check, hours) for an interface read, or an error Response to return."""
    try:
        device = UnmanagedDevice.objects.get(pk=device_id)
        check = DeviceCheck.objects.get(pk=check_id, device=device)
    except (UnmanagedDevice.DoesNotExist, DeviceCheck.DoesNotExist):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    try:
        hours = int(request.query_params.get("hours", 24))
    except (TypeError, ValueError):
        hours = 0
    if not 1 <= hours <= 720:
        return Response({"error": "hours must be a whole number from 1 to 720"},
                        status=status.HTTP_400_BAD_REQUEST)
    return check, hours


def _rate(newer, older, field):
    """Bits per second between two samples of one interface, or None.

    No previous sample, a counter the device never reported, no elapsed time
    or a counter that went backwards — a reset or a reboot — all mean the same
    thing: this interval has no rate to report.
    """
    if older is None:
        return None
    high = getattr(newer, field)
    low = getattr(older, field)
    if high is None or low is None:
        return None
    seconds = (newer.time - older.time).total_seconds()
    if seconds <= 0 or high < low:
        return None
    return round((high - low) * 8 / seconds)


@extend_schema(methods=["GET"],
               parameters=[OpenApiParameter(
                   "hours", int, required=False,
                   description="How far back to read, 1 to 720 hours. Default 24.")],
               responses={200: HostsDeviceCheckInterfaceSerializer(many=True),
                          400: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def device_check_interfaces(request, device_id, check_id):
    """What each interface of an SNMP check was doing at its newest sample.

    The newest row of every interface in the window, with its rate over the gap
    behind it — see `_rate`, which both these views use so the answer to "no
    usable pair" is written once. A check of another kind answers `[]`: an empty
    table and no interfaces are the same thing to a chart.
    """
    found = _check_window(request, device_id, check_id)
    if isinstance(found, Response):
        return found
    check, hours = found
    if check.kind != DeviceCheck.Kind.SNMP:
        # Samples only ever come from an SNMP check's walk, so a check of any
        # other kind has no interfaces to answer with.
        return Response([])
    since = now() - timedelta(hours=hours)
    # Only the two newest runs matter here: the newest sample and the one behind
    # it. Reading the whole window would be up to 720 h x 64 interfaces of rows.
    runs = list(check.interface_samples.filter(time__gte=since).order_by("-time")
                .values_list("time", flat=True).distinct()[:2])
    rows = list(check.interface_samples.filter(time__in=runs).order_by("if_index", "time"))
    newest: dict[int, list] = {}
    for row in rows:
        newest.setdefault(row.if_index, []).append(row)
    out = []
    for index in sorted(newest):
        seen = newest[index]
        latest = seen[-1]
        out.append({"time": latest.time, "index": index, "name": latest.name,
                    "up": latest.up, "speed_mbps": latest.speed_mbps,
                    "in_bps": _rate(latest, seen[-2] if len(seen) > 1 else None, "in_octets"),
                    "out_bps": _rate(latest, seen[-2] if len(seen) > 1 else None, "out_octets")})
    return Response(out)


@extend_schema(methods=["GET"],
               parameters=[OpenApiParameter(
                   "hours", int, required=False,
                   description="How far back to read, 1 to 720 hours. Default 24.")],
               responses={200: HostsDeviceCheckInterfaceSeriesSerializer,
                          400: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def device_check_interface(request, device_id, check_id, if_index):
    """One interface's rate over the window, oldest first: a point per pair of samples."""
    found = _check_window(request, device_id, check_id)
    if isinstance(found, Response):
        return found
    check, hours = found
    rows = list(check.interface_samples
                .filter(if_index=if_index, time__gte=now() - timedelta(hours=hours))
                .order_by("-time")[:MAX_SERIES_POINTS])
    if not rows:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    rows.reverse()
    series = [{"time": newer.time,
               "in_bps": _rate(newer, older, "in_octets"),
               "out_bps": _rate(newer, older, "out_octets")}
              for older, newer in itertools.pairwise(rows)]
    return Response({"index": if_index, "name": rows[-1].name, "series": series})


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
