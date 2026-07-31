"""Sites API — reads for everyone logged in (they power the greyed preview),
writes gated by ``require_feature("sites")``: 402 + upgrade body, never a bare
403 (SQSY-LICENSING.md §5). Nothing here is reachable from agent checkin —
licensing can never touch monitoring."""

from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.contrib.auth import get_user_model

from apps.accounts.models import Role
from apps.accounts.permissions import IsAdmin
from apps.hosts.models import Host
from vigil.licensing import require_feature

from .models import (
    HostSiteAssignment, Site, SiteCapability, UserSiteRole,
)
from .serializers import SiteSerializer


def _gate(request):
    """The write gate. Returns a 402 Response, or None when licensed."""
    from rest_framework.exceptions import APIException

    perm = require_feature("sites")()
    try:
        perm.has_permission(request, None)
    except APIException as exc:
        return Response(exc.detail, status=402)
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def site_index(request):
    if request.method == "POST":
        denied = _gate(request)
        if denied is not None:
            return denied
        ser = SiteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)

    sites = list(Site.objects.annotate(host_count=Count("host_assignments")))
    # Hosts with no assignment belong to the default site.
    unassigned = Host.objects.filter(site_assignment__isnull=True).count()
    for s in sites:
        if s.is_global:
            s.host_count += unassigned
    return Response(SiteSerializer(sites, many=True).data)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def site_detail(request, site_id):
    try:
        site = Site.objects.annotate(host_count=Count("host_assignments")).get(pk=site_id)
    except Site.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(SiteSerializer(site).data)

    denied = _gate(request)
    if denied is not None:
        return denied

    if request.method == "PATCH":
        ser = SiteSerializer(site, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    if site.is_global:
        return Response(
            {"detail": "The global site cannot be deleted."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Hosts of a deleted site fall back to the default site: assignment rows
    # vanish with the site (CASCADE) and absence means "default".
    site.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["PUT", "DELETE"])
@permission_classes([IsAuthenticated, require_feature("sites")])
def site_host_assignment(request, site_id, host_id):
    try:
        site = Site.objects.get(pk=site_id)
        host = Host.objects.get(pk=host_id)
    except (Site.DoesNotExist, Host.DoesNotExist):
        return Response(status=status.HTTP_404_NOT_FOUND)

    if request.method == "PUT":
        HostSiteAssignment.objects.update_or_create(host=host, defaults={"site": site})
        return Response({"host": str(host.pk), "site": str(site.pk)})

    HostSiteAssignment.objects.filter(host=host, site=site).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


def _rbac_gate(request):
    """Write gate for per-site roles. Editing is a Business feature; *reading*
    an existing grant never is, and neither is resolution — revoking people's
    access on lapse would be an outage, not a downgrade (SQSY-LICENSING §6)."""
    from rest_framework.exceptions import APIException

    perm = require_feature("rbac_advanced")()
    try:
        perm.has_permission(request, None)
    except APIException as exc:
        return Response(exc.detail, status=402)
    return None


def _role_row(row):
    return {
        "id": row.id,
        "user": row.user_id,
        "username": row.user.username,
        "site": str(row.site_id),
        "site_name": row.site.name,
        "role": row.role,
        "capabilities": sorted(
            [c.app, c.verb] for c in row.capabilities.all()),
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def user_site_roles(request):
    """List or grant per-site roles.

    GET  ?user=<id> — that user's grants, or every grant when omitted.
    POST {user, site, role, capabilities: [[app, verb], ...]}
    """
    if request.method == "GET":
        qs = UserSiteRole.objects.select_related("user", "site").prefetch_related(
            "capabilities").order_by("user__username", "site__name")
        wanted = request.query_params.get("user")
        if wanted:
            qs = qs.filter(user_id=wanted)
        return Response([_role_row(r) for r in qs])

    denied = _rbac_gate(request)
    if denied is not None:
        return denied

    user_id = request.data.get("user")
    site_id = request.data.get("site")
    role = (request.data.get("role") or "").strip()
    if role not in Role.values:
        return Response(
            {"detail": f"role must be one of: {', '.join(Role.values)}"}, status=400)

    user = get_user_model().objects.filter(pk=user_id).first()
    site = Site.objects.filter(pk=site_id).first()
    if user is None or site is None:
        return Response({"detail": "unknown user or site"}, status=400)

    row, _ = UserSiteRole.objects.update_or_create(
        user=user, site=site, defaults={"role": role})
    err = _set_capabilities(row, request.data.get("capabilities"))
    if err is not None:
        return Response({"detail": err}, status=400)
    row.refresh_from_db()
    return Response(_role_row(row), status=status.HTTP_201_CREATED)


def _set_capabilities(row, raw):
    """Replace a grant's capabilities. Returns an error string or None."""
    from apps.accounts.permissions import CAPABILITIES

    if raw is None:
        return None
    if not isinstance(raw, list):
        return "capabilities must be a list of [app, verb] pairs"

    pairs = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return f"malformed capability {entry!r}"
        app, verb = str(entry[0]), str(entry[1])
        verbs = CAPABILITIES.get(app)
        if verbs is None or verb not in verbs:
            return f"unknown capability {app}:{verb}"
        pairs.append((app, verb))

    row.capabilities.all().delete()
    SiteCapability.objects.bulk_create(
        [SiteCapability(user_site_role=row, app=a, verb=v) for a, v in set(pairs)])
    return None


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def user_site_role_detail(request, role_id):
    """Revoke a grant. Capabilities cascade away with it.

    Revoking is deliberately NOT gated: a lapsed licence must never stop an
    administrator from removing someone's access.
    """
    row = UserSiteRole.objects.filter(pk=role_id).first()
    if row is None:
        return Response(status=status.HTTP_404_NOT_FOUND)

    # Removing someone's last grant returns them to their profile role, which
    # is wider, not narrower — so there is no lockout to guard against here.
    row.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
