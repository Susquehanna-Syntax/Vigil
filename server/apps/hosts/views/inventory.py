"""apps.hosts.views: inventory views (split from the old single module, unchanged)."""
# ruff: noqa: I001
# The star import stays first of this block on purpose: _common is where these
# views get Response, api_view and the permission classes.
from ._common import *  # noqa: F401,F403

# `extend_schema` comes last of the block so the star import above stays where
# this package has always had it.
from ..api_schema import (HostsContainerSerializer, HostsErrorSerializer,
                          HostsInventoryListSerializer, HostsInventoryRowSerializer,
                          HostsStackSerializer, HostsTagCountSerializer)
from drf_spectacular.utils import extend_schema


@extend_schema(responses={200: HostsTagCountSerializer(many=True)})
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


@extend_schema(responses={200: HostsInventoryListSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("inventory")
def inventory_list(request):
    """Inventory rows for every non-rejected host.

    Hosts with no inventory yet are still returned (with empty fields) so
    the UI can show "no agent data yet" alongside actual entries.
    """
    hosts = (
        scoping.filter_by_site(Host.objects.exclude(status=Host.Status.REJECTED), request.user)
        .select_related("inventory")
        .order_by("hostname")
    )
    rows = []
    seen_columns: set[str] = set()
    containers = _container_counts()
    for host in hosts:
        inv = getattr(host, "inventory", None)
        if inv is None:
            inv = HostInventory(host=host)  # in-memory placeholder, not saved
        row = HostInventorySerializer(inv).data
        row.update(containers.get(host.id, _NO_CONTAINERS))
        rows.append(row)
        custom = inv.custom_columns or {}
        seen_columns.update(custom.keys())
    return Response({"rows": rows, "custom_columns": sorted(seen_columns)})


@extend_schema(responses={200: HostsInventoryRowSerializer, 404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def inventory_detail(request, host_id):
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    inv = getattr(host, "inventory", None) or HostInventory(host=host)
    return Response(HostInventorySerializer(inv).data)


@extend_schema(responses={200: HostsStackSerializer(many=True), 404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_stacks(request, host_id):
    """Compose stacks on one host (M11): who owns each, where its compose
    file lives, and how many of its containers run."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    return Response([{
        "id": s.id, "project": s.project, "ownership": s.ownership,
        "config_files": s.config_files, "working_dir": s.working_dir, "engine": s.engine,
        "container_count": s.container_count, "running_count": s.running_count,
        "seen_at": s.seen_at.isoformat() if s.seen_at else None,
    } for s in host.container_stacks.all()])


@extend_schema(responses={200: HostsContainerSerializer(many=True), 404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_containers(request, host_id):
    """Docker containers reported for one host, ordered by stack then name."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    from ..models import ContainerImageHistory, ContainerRollback

    qs = host.docker_containers.all()
    previous: dict = {}
    for h in ContainerImageHistory.objects.filter(host=host):   # newest first
        previous.setdefault((h.host_id, h.container_name), h.image_digest or h.image_id)
    rolled = {(r.host_id, r.container_name): r.image for r in ContainerRollback.objects.filter(host=host)}
    return Response(DockerContainerSerializer(
        qs, many=True, context={"outdated": _open_outdated([host.id]), "previous": previous,
                                "rolled_back": rolled}
    ).data)


@extend_schema(responses={200: HostsContainerSerializer(many=True)})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def docker_overview(request):
    """Every container across all hosts — the monitor page's Docker view."""
    qs = (
        DockerContainer.objects.select_related("host")
        .all()
        .order_by("host__hostname", "stack", "name")
    )
    return Response(DockerContainerSerializer(
        qs, many=True, context={"outdated": _open_outdated()}
    ).data)
