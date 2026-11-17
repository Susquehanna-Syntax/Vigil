"""The discovery API (S9 06d): queue a scan on a poller, read what it found,
adopt the answered hosts as devices.

Reads are open to any role that can see devices; every write is admin-only and,
on a site install, needs write access to the scan's site, which is its poller's
(``site_for_write`` / ``write_denied``). A poller of a site the user cannot see
is simply not found. The SNMP community is never read back: ``snmp`` says only
whether one is stored.

The shapes are written out here rather than in ``api_schema``, because two of
them name what lives on another row — a scan's poller hostname, its count of
found hosts — and no serializer field can read that off the scan itself. The
serializers below and the ``_out`` function beside each say the same thing; an
answer carrying a key either lacks fails a test in ``test_discovery_api.py``.
"""
# ruff: noqa: I001
# The star import stays first of this block on purpose: _common is where these
# views get Response, api_view, status, IsAdmin, scoping, transaction and
# encrypt_secret.
import uuid

from ._common import *

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from rest_framework import serializers

from drf_spectacular.utils import extend_schema

from ..discovery import MAX_OPEN_SCANS_PER_POLLER, validate_cidr, validate_ports
from ..models import DiscoveryScan, DiscoveredHost


#: What the write endpoints answer a role that is not an administrator, and one
#: whose body is not an object. Refusals all say a single sentence.
_NOT_ADMIN = {"error": "Administrator access required."}
_BAD_BODY = {"error": "The body must be an object."}
_NO_SCAN = {"error": "Not found"}
_BAD_HOSTS = {"error": "hosts must name hosts of this scan"}
_NOT_DONE = {"error": "Only a finished scan can be adopted from."}


class _ErrorResponse(serializers.Serializer):
    """A refusal: one sentence, whatever the endpoint refused."""

    error = serializers.CharField()


def _is_admin(request) -> bool:
    return IsAdmin().has_permission(request, None)


def _has_sites() -> bool:
    """Whether this install has sites at all.

    The site manager's filter fails closed with none — a user of no assignment
    belongs to no site and sees no site-scoped row, right on a site install and
    the opposite of what Community means — so a read that must answer both asks."""
    return scoping._sites_models() is not None


# ── shapes ────────────────────────────────────────────────────────────────────

class _PollerSerializer(serializers.Serializer):
    """A host the page can offer as a poller. ``online`` is whether it answered
    its last check-in; an offline one still takes a scan, for its next call home."""

    id = serializers.CharField()
    hostname = serializers.CharField()
    site_id = serializers.CharField(allow_null=True)
    online = serializers.BooleanField()


class _ScanPollerSerializer(serializers.Serializer):
    """The poller a scan runs on, as a scan answer names it."""

    id = serializers.CharField()
    hostname = serializers.CharField()


class _KnownDeviceSerializer(serializers.Serializer):
    """The device a found host is already recorded as."""

    id = serializers.CharField()
    name = serializers.CharField()


class _DiscoveredHostSerializer(serializers.Serializer):
    """One host the poller answered with. ``known`` names the device it already
    is — one of this scan's site, or a global one — or is null."""

    id = serializers.IntegerField()
    ip = serializers.IPAddressField()
    alive_by = serializers.CharField()
    latency_ms = serializers.FloatField(allow_null=True)
    open_ports = serializers.ListField(child=serializers.IntegerField())
    name = serializers.CharField()
    sys_name = serializers.CharField()
    sys_descr = serializers.CharField()
    known = _KnownDeviceSerializer(allow_null=True)


class _ScanSerializer(serializers.Serializer):
    """One scan. Never the community: ``snmp`` says only that one is stored."""

    id = serializers.CharField()
    poller = _ScanPollerSerializer()
    cidr = serializers.CharField()
    ports = serializers.ListField(child=serializers.IntegerField())
    state = serializers.CharField()
    created_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField()
    scanned = serializers.IntegerField()
    truncated = serializers.BooleanField()
    snmp = serializers.BooleanField()
    found = serializers.IntegerField()
    requested_by = serializers.CharField(allow_null=True)


class _ScanDetailSerializer(_ScanSerializer):
    """A scan and what its poller answered. The list carries the ``found`` count
    alone; the rows are read one scan at a time."""

    hosts = serializers.ListField(child=_DiscoveredHostSerializer())


class _ScanBodySerializer(serializers.Serializer):
    poller = serializers.CharField()
    cidr = serializers.CharField()
    ports = serializers.ListField(child=serializers.IntegerField(), required=False)
    community = serializers.CharField(required=False, allow_blank=True)


class _AdoptBodySerializer(serializers.Serializer):
    hosts = serializers.ListField(child=serializers.IntegerField())
    device_type = serializers.ChoiceField(choices=UnmanagedDevice.DeviceType.choices,
                                          required=False)


class _AdoptedDeviceSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    ip_address = serializers.IPAddressField()


class _AdoptResultSerializer(serializers.Serializer):
    created = serializers.ListField(child=_AdoptedDeviceSerializer())
    skipped = serializers.IntegerField()


# ── the picker ────────────────────────────────────────────────────────────────

@extend_schema(methods=["GET"], responses={200: _PollerSerializer(many=True)})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def discovery_pollers(request):
    """The hosts that can run a scan — the page's poller picker."""
    return Response([_poller_out(host) for host in _pickable_pollers(request.user)])


def _poller_out(host: Host) -> dict:
    return {"id": str(host.id), "hostname": host.hostname,
            "site_id": _poller_site_id(host),
            # The pick shows whether the scan starts now or at the next check-in.
            "online": host.status == Host.Status.ONLINE}


def _pickable_pollers(user):
    """The hosts *user* may offer a scan to.

    One read of the manager's filter, which already holds both rules: on a site
    install whose pollers *user* can see is whose they may queue a scan on — only
    an administrator of the whole install reaches past the filter to one of the
    Global site's — and no user this install does not know sees any row at all."""
    hosts = list(Host.objects.filter(agent_poller=True))
    scoping.prime_site_ids(hosts)
    return sorted(hosts, key=lambda host: host.hostname)


# ── the scans ─────────────────────────────────────────────────────────────────


def _row_site_id(row):
    """The site *row* was placed in, read off its own column (None on Community)."""
    return row.site_id


def _poller_site_id(host: Host):
    """The site *host* runs in — its assignment's, which is one hop away and so is
    read through the lookup that knows how. None where nothing says: a host of no
    site, or any host at all on an install without sites."""
    if not _has_sites():
        return None
    return scoping.site_id_of(host)


def _scan_out(scan: DiscoveryScan, found=None) -> dict:
    """A scan as both reads render it. Never the community.

    ``found`` is passed in by the detail answer, which has already read those rows
    and has no reason to count them a second time."""
    return {
        "id": str(scan.id),
        "poller": {"id": str(scan.poller_id), "hostname": scan.poller.hostname},
        "cidr": scan.cidr,
        "ports": scan.ports,
        "state": scan.state,
        "created_at": scan.created_at,
        "sent_at": scan.sent_at,
        "finished_at": scan.finished_at,
        "error": scan.error,
        "scanned": scan.scanned,
        "truncated": scan.truncated,
        "snmp": bool(scan.secret),
        "found": found if found is not None else _found_count(scan),
        "requested_by": scan.requested_by.username if scan.requested_by_id else None,
    }


def _found_count(scan: DiscoveryScan) -> int:
    """The list's annotated count when there is one, else a count of its own."""
    annotated = getattr(scan, "found_count", None)
    return scan.hosts.count() if annotated is None else annotated


def _newest_scans():
    """The 50 newest scans in this user's scope, poller, requester and found count
    read in one query."""
    return list(DiscoveryScan.objects.select_related("poller", "requested_by")
                .annotate(found_count=Count("hosts")).order_by("-created_at")[:50])


def _scan_by_id(scan_id):
    """The scan *scan_id* names inside this user's scope, or None.

    Out of scope reads as absent: a 403 would confirm that another site queued a
    scan on some poller."""
    try:
        wanted = uuid.UUID(str(scan_id))
    except ValueError:
        return None
    try:
        return DiscoveryScan.objects.select_related("poller", "requested_by").get(pk=wanted)
    except DiscoveryScan.DoesNotExist:
        return None


@extend_schema(methods=["GET"], responses={200: _ScanSerializer(many=True)})
@extend_schema(methods=["POST"], request=_ScanBodySerializer,
               responses={201: _ScanSerializer, 400: _ErrorResponse,
                          403: _ErrorResponse, 409: _ErrorResponse})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def discovery_scans(request):
    """The newest scans this user can see, or queue one (admin)."""
    if request.method == "GET":
        return Response([_scan_out(scan) for scan in _newest_scans()])

    if not _is_admin(request):
        return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
    if not isinstance(request.data, dict):
        return Response(_BAD_BODY, status=status.HTTP_400_BAD_REQUEST)

    poller = _chosen_poller(request.data.get("poller"))
    if poller is None:
        return Response({"error": "poller must be a poller host"},
                        status=status.HTTP_400_BAD_REQUEST)
    # The scan is placed in the poller's site, and whether this user may write
    # there is exactly what site_for_write answers: another site's admin cannot
    # queue work on its machines.
    site_id, denied = scoping.site_for_write(request.user, _poller_site_id(poller))
    if denied:
        return denied
    try:
        cidr = validate_cidr(request.data.get("cidr"))
        ports = validate_ports(request.data.get("ports"))
        community = _community(request.data.get("community"))
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    waiting = _open_scan_count(poller)
    if waiting >= MAX_OPEN_SCANS_PER_POLLER:
        return Response({"error": f"This poller already has {MAX_OPEN_SCANS_PER_POLLER} "
                                  "scans waiting."},
                        status=status.HTTP_409_CONFLICT)
    scan = DiscoveryScan.objects.create(
        poller=poller, site_id=site_id, cidr=cidr, ports=ports,
        secret=encrypt_secret(community) if community is not None else b"",
        requested_by=request.user)
    return Response(_scan_out(scan), status=status.HTTP_201_CREATED)


def _open_scan_count(poller) -> int:
    """How many of *poller*'s scans are waiting to run, whoever queued them.

    The cap is the agent's: a scan this user cannot see still takes one of the
    poller's slots."""
    return DiscoveryScan.objects.unscoped(
        reason="discovery cap: a poller's waiting scans, whoever queued them",
    ).filter(poller=poller, state__in=[DiscoveryScan.State.QUEUED,
                                       DiscoveryScan.State.SENT]).count()


def _chosen_poller(raw):
    """The host *raw* names that this user may offer a scan to, else None.

    One read of the manager's filter, which is where both rules already live: on a
    site install another site's poller is simply not there, and neither is any row
    at all for someone this install does not know."""
    try:
        host = Host.objects.get(pk=raw)
    except (Host.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        return None
    return host if host.agent_poller else None


def _community(raw):
    """The community text to store, or None when the body gave none.

    An empty or absent one is none: the scan then does no SNMP at all. Anything
    else must be short printable text, and goes into ``secret`` encrypted."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or len(raw) > 64 \
            or not all(32 <= ord(c) <= 126 for c in raw):
        raise ValueError("community must be text of at most 64 characters")
    return raw


@extend_schema(methods=["GET"], responses={200: _ScanDetailSerializer, 404: _ErrorResponse})
@extend_schema(methods=["DELETE"], responses={204: None, 403: _ErrorResponse,
                                              404: _ErrorResponse})
@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def discovery_scan(request, scan_id):
    """One scan with the hosts it found, or throw it away (admin)."""
    scan = _scan_by_id(scan_id)
    if scan is None:
        return Response(_NO_SCAN, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        if not _is_admin(request):
            return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
        if denied := scoping.write_denied(request.user, scan):
            return denied
        scan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    rows = list(scan.hosts.order_by("ip"))
    known = _known_devices(scan)
    out = _scan_out(scan, found=len(rows))
    out["hosts"] = [dict(_host_out(row), known=known.get(row.ip)) for row in rows]
    return Response(out)


def _known_devices(scan: DiscoveryScan) -> dict:
    """``{ip: {"id", "name"}}`` for the device each found host already is.

    The manager's filter holds this to the user's own sites; on top of it a scan's
    results are matched against what *its* site sees — its own devices and the
    global ones, since a device with no site applies everywhere — so a same-address
    device of a third site can neither hide a find nor be named in an answer here;
    on Community every device is unassigned and so in scope for every scan. One
    query for the whole scan, not one per found host."""
    site_id = _row_site_id(scan)
    rows = _device_rows(site_id)
    known = {}
    for row in rows:
        if row["ip_address"]:
            known.setdefault(row["ip_address"], {"id": str(row["id"]), "name": row["name"]})
    return known


def _device_rows(site_id):
    """The devices a scan of site *site_id* matches its hosts against.

    Its own site's, and the global ones — a device with no site applies to every
    network — which one query answers for a whole scan. Where a scan names no site
    there is nothing to match against, so every device in this user's scope can be
    the one a found host already is."""
    devices = UnmanagedDevice.objects.exclude(ip_address__isnull=True)
    wanted = devices if site_id is None else devices.filter(
        Q(site_id=site_id) | Q(site_id__isnull=True))
    return list(wanted.values("id", "name", "ip_address"))


def _host_out(row: DiscoveredHost) -> dict:
    return {
        "id": row.id,
        "ip": row.ip,
        "alive_by": row.alive_by,
        "latency_ms": row.latency_ms,
        "open_ports": row.open_ports,
        "name": row.name,
        "sys_name": row.sys_name,
        "sys_descr": row.sys_descr,
    }


# ── adopting what it found ────────────────────────────────────────────────────

@extend_schema(methods=["POST"], request=_AdoptBodySerializer,
               responses={200: _AdoptResultSerializer, 400: _ErrorResponse,
                          403: _ErrorResponse, 409: _ErrorResponse})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@scoping.module_view("devices")
def discovery_adopt(request, scan_id):
    """Turn hosts a finished scan found into devices (admin).

    Each becomes a device of the scan's site, named after what it answered with
    and carrying a note of where it came from. One an existing device already
    covers is skipped rather than duplicated."""
    scan = _scan_by_id(scan_id)
    if scan is None:
        return Response(_NO_SCAN, status=status.HTTP_404_NOT_FOUND)
    if not _is_admin(request):
        return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
    if denied := scoping.write_denied(request.user, scan):
        return denied
    if not isinstance(request.data, dict):
        return Response(_BAD_BODY, status=status.HTTP_400_BAD_REQUEST)
    if scan.state != DiscoveryScan.State.DONE:
        return Response(_NOT_DONE, status=status.HTTP_409_CONFLICT)

    device_type = request.data.get("device_type") or UnmanagedDevice.DeviceType.OTHER
    if device_type not in UnmanagedDevice.DeviceType.values:
        return Response({"error": "device_type must be one of the device types"},
                        status=status.HTTP_400_BAD_REQUEST)
    rows = _named_hosts(scan, request.data.get("hosts"))
    if not rows:
        return Response(_BAD_HOSTS, status=status.HTTP_400_BAD_REQUEST)

    known = _known_devices(scan)
    site_id = _row_site_id(scan)
    created = []
    with transaction.atomic():
        for row in rows:
            # A device already recorded at that address is not a new find.
            if row.ip in known:
                continue
            device = UnmanagedDevice.objects.create(
                site_id=site_id, name=_device_name(row), ip_address=row.ip,
                device_type=device_type, notes=_adoption_notes(scan, row))
            created.append({"id": str(device.id), "name": device.name,
                            "ip_address": device.ip_address})
    return Response({"created": created, "skipped": len(rows) - len(created)})


def _named_hosts(scan: DiscoveryScan, raw):
    """The rows of *scan* its body names: at most 256 ids in whole, hosts of some
    other scan and junk entries ignored. Nothing left is the caller's refusal."""
    if not isinstance(raw, list) or len(raw) > 256:
        return []
    ids = [entry for entry in raw if isinstance(entry, int) and not isinstance(entry, bool)]
    return list(scan.hosts.filter(id__in=ids).order_by("id"))


def _device_name(row: DiscoveredHost) -> str:
    """What to call an adopted host: the name SNMP read from it, then the one
    its answer carried, then its address."""
    for candidate in (row.sys_name, row.name, row.ip):
        if candidate:
            return candidate[:200]
    return ""


def _adoption_notes(scan: DiscoveryScan, row: DiscoveredHost) -> str:
    """Where the device came from, so a later reader can trace it back."""
    notes = f"Found by a discovery scan of {scan.cidr} on {scan.created_at:%Y-%m-%d}."
    if row.sys_descr:
        notes += f"\n{row.sys_descr}"
    return notes
