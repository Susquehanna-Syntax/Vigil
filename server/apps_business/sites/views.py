"""Sites API — reads for everyone logged in (they power the greyed preview),
writes gated by ``require_feature("sites")``: 402 + upgrade body, never a bare
403 (SQSY-LICENSING.md §5). Nothing here is reachable from agent checkin —
licensing can never touch monitoring."""

from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.hosts.models import Host
from vigil.licensing import require_feature

from .models import HostSiteAssignment, Site
from .serializers import SiteSerializer


def _gate(request):
    """The write gate. Returns a 402 Response, or None when licensed."""
    perm = require_feature("sites")()
    if perm.has_permission(request, None):
        return None
    return Response(perm.message, status=402)


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
        if s.is_default:
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

    if site.is_default:
        return Response(
            {"detail": "The default site cannot be deleted."},
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
