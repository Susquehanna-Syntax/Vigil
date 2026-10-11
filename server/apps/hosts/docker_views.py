"""Images, volumes and networks for one host (14.3) — the Containers page's
Images / Volumes / Networks tabs. Which containers use each object comes
from the containers' own edges (DockerContainer.networks / .mounts /
.image_id), so "unused" is computed, not guessed."""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping

from .models import DockerContainer, DockerImage, DockerNetwork, DockerVolume, HostDockerUsage

_BUILTIN_NETWORKS = {"bridge", "host", "none", "podman"}


def _engine_kind(engines) -> str:
    """The first engine's kind; agents report a list of dicts, older ones of names."""
    first = (engines or [None])[0] if isinstance(engines, list) else None
    if isinstance(first, dict):
        return str(first.get("kind") or "")[:20]
    return str(first or "")[:20]


def _short(image_id: str) -> str:
    return image_id.split(":", 1)[-1][:12]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_docker(request, host_id):
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    containers = list(DockerContainer.objects.filter(host=host)
                      .values("name", "image_id", "state", "stack", "networks", "mounts"))
    usage = HostDockerUsage.objects.filter(host=host).first()

    by_image: dict = {}
    by_volume: dict = {}
    by_network: dict = {}
    for c in containers:
        if c["image_id"]:
            by_image.setdefault(c["image_id"], []).append(c["name"])
        for m in c["mounts"] or []:
            if m.get("type") == "volume" and m.get("name"):
                by_volume.setdefault(m["name"], []).append(
                    {"container": c["name"], "destination": m.get("destination", ""), "rw": m.get("rw", True)})
        for net in c["networks"] or []:
            if net.get("name"):
                by_network.setdefault(net["name"], []).append({"container": c["name"], "ip": net.get("ip", ""),
                                                               "state": c["state"]})

    images = []
    for i in DockerImage.objects.filter(host=host).order_by("-size_bytes"):
        used = by_image.get(i.image_id, [])
        images.append({"id": i.image_id, "short_id": _short(i.image_id), "tags": i.tags, "digests": i.digests,
                       "size": i.size_bytes, "created": i.created.isoformat() if i.created else None,
                       "used_by": used, "dangling": not i.tags})
    sizes = (usage.volume_sizes if usage else {}) or {}
    volumes = [{"name": v.name, "driver": v.driver, "stack": v.stack, "mountpoint": v.mountpoint,
                "created": v.created, "size": sizes.get(v.name), "used_by": by_volume.get(v.name, [])}
               for v in DockerVolume.objects.filter(host=host).order_by("name")]
    networks = [{"id": n.network_id, "name": n.name, "driver": n.driver, "scope": n.scope,
                 "internal": n.internal, "subnets": n.subnets, "stack": n.stack,
                 "builtin": n.name in _BUILTIN_NETWORKS, "containers": by_network.get(n.name, [])}
                for n in DockerNetwork.objects.filter(host=host).order_by("name")]
    return Response({
        "snapshot_at": host.docker_snapshot_at.isoformat() if host.docker_snapshot_at else None,
        "usage": ({k: getattr(usage, k) for k in (
            "images_size", "images_reclaimable", "containers_size", "volumes_size", "volumes_reclaimable",
            "build_cache_size", "build_cache_reclaimable")} | {"read_at": usage.read_at.isoformat()}
                  if usage else None),
        "images": images, "volumes": volumes, "networks": networks,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_docker_summary(request):
    """Every host the user may see, with its container count — the
    Containers page's host picker lists hosts that run containers first."""
    from django.db.models import Count, Q

    from .models import Host

    hosts = (scoping.filter_by_site(Host.objects.exclude(status__in=("rejected", "pending")), request.user)
             .annotate(containers=Count("docker_containers", distinct=True),
                       running=Count("docker_containers", filter=Q(docker_containers__state="running"),
                                     distinct=True))
             .order_by("-containers", "hostname"))
    return Response([{"id": str(h.id), "hostname": h.hostname, "os": h.os, "status": h.status,
                      "containers": h.containers, "running": h.running,
                      "engine": _engine_kind(h.container_engines)}
                     for h in hosts])
