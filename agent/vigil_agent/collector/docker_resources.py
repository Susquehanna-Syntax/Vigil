"""Images, volumes and networks on this engine.

One call per list endpoint, no per-item inspect: the fleet view renders these
as counts plus a few names, so a host with two hundred containers should not pay
two hundred round-trips to report them. Returns None when there is no engine —
"no Docker here" and "Docker answered nothing" are different answers.
"""

from ._common import logger
from .docker_engine import _engine_available, _docker_api_get


def collect_docker_resources() -> dict | None:
    """Images, volumes and networks for the check-in (14.3). Three cheap list
    calls; sizes beyond each image's own come from the on-demand
    ``docker_disk_usage`` action. None when the engine is unavailable."""
    if not _engine_available():
        return None
    try:
        images = _docker_api_get("/images/json") or []
        volumes = (_docker_api_get("/volumes") or {}).get("Volumes") or []
        networks = _docker_api_get("/networks") or []
    except Exception as exc:  # noqa: BLE001 — optional, never break a check-in
        logger.debug("Docker resource list failed: %s", exc)
        return None
    return {
        "images": [{
            "id": str(i.get("Id") or "")[:80],
            "tags": [str(t)[:255] for t in (i.get("RepoTags") or []) if t and t != "<none>:<none>"][:16],
            "digests": [str(d)[:300] for d in (i.get("RepoDigests") or [])][:4],
            "size": int(i.get("Size") or 0),
            "created": int(i.get("Created") or 0),
        } for i in images[:2000] if isinstance(i, dict)],
        "volumes": [{
            "name": str(v.get("Name") or "")[:255],
            "driver": str(v.get("Driver") or "")[:64],
            "mountpoint": str(v.get("Mountpoint") or "")[:500],
            "created": str(v.get("CreatedAt") or "")[:40],
            "stack": str((v.get("Labels") or {}).get("com.docker.compose.project") or "")[:200],
        } for v in volumes[:2000] if isinstance(v, dict)],
        "networks": [{
            "id": str(n.get("Id") or "")[:64],
            "name": str(n.get("Name") or "")[:255],
            "driver": str(n.get("Driver") or "")[:64],
            "scope": str(n.get("Scope") or "")[:16],
            "internal": bool(n.get("Internal")),
            "subnets": [str(c.get("Subnet") or "")[:64]
                        for c in ((n.get("IPAM") or {}).get("Config") or []) if isinstance(c, dict)][:8],
            "stack": str((n.get("Labels") or {}).get("com.docker.compose.project") or "")[:200],
        } for n in networks[:500] if isinstance(n, dict)],
    }

