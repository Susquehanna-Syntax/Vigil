"""Images, volumes and networks (14.3): how much space they take, and
removing or pruning them, through the engine API.

The lists themselves travel with every check-in (collector
``collect_docker_resources``); measuring sizes is the slow part, so it is an
action of its own, ``docker_disk_usage``, run when someone asks. Removing a
volume deletes data, so the two volume actions are high risk on the server.
"""
from __future__ import annotations

import json
import re

from .. import collector
from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _SAFE_IMAGE

#: A volume or network name as docker allows it, or a full 64-hex id.
_SAFE_OBJECT = re.compile(r"^(?:[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}|[0-9a-f]{64})$")
#: An image id (sha256:…, or its short hex form) or a reference.
_IMAGE_ID = re.compile(r"^(?:sha256:)?[0-9a-f]{12,64}$")
#: The networks every engine creates; removing one breaks the engine.
_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none", "podman"})


def _engine():
    return ex._engine()


def _object_name(params: dict, key: str, label: str) -> str:
    value = str(params.get(key) or "")
    if not _SAFE_OBJECT.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _flag(params: dict, key: str) -> bool:
    value = params.get(key)
    if value in (None, "", False, "false", "False"):
        return False
    if value in (True, "true", "True"):
        return True
    raise ValueError(f"{key} must be true or false")


def _docker_disk_usage(params: dict, _config: AgentConfig) -> str:
    """``docker system df -v`` as JSON: totals per kind, and each volume's and
    image's size, for the Images and Volumes tabs. Slow on big hosts (the
    engine walks every volume), so it runs on demand."""
    df = _engine().get("/system/df", timeout=300) or {}
    images = df.get("Images") or []
    volumes = df.get("Volumes") or []
    containers = df.get("Containers") or []
    cache = df.get("BuildCache") or []

    def vsize(v):
        size = ((v.get("UsageData") or {}).get("Size"))
        return size if isinstance(size, int) and size >= 0 else None

    def vrefs(v):
        refs = (v.get("UsageData") or {}).get("RefCount")
        return refs if isinstance(refs, int) and refs >= 0 else None

    summary = {
        "kind": "docker_disk_usage",
        "images_size": int(df.get("LayersSize") or 0),
        "images_reclaimable": sum(int(i.get("Size") or 0) - int(i.get("SharedSize") or 0)
                                  for i in images if int(i.get("Containers") or 0) == 0
                                  and int(i.get("Size") or 0) >= int(i.get("SharedSize") or 0)),
        "containers_size": sum(int(c.get("SizeRw") or 0) for c in containers),
        "volumes_size": sum(vsize(v) or 0 for v in volumes),
        "volumes_reclaimable": sum(vsize(v) or 0 for v in volumes if vrefs(v) == 0),
        "build_cache_size": sum(int(b.get("Size") or 0) for b in cache),
        "build_cache_reclaimable": sum(int(b.get("Size") or 0) for b in cache if not b.get("InUse")),
        "images": [{"id": str(i.get("Id") or "")[:80], "size": int(i.get("Size") or 0),
                    "shared_size": int(i.get("SharedSize") or 0), "containers": int(i.get("Containers") or 0)}
                   for i in images[:2000]],
        "volumes": [{"name": str(v.get("Name") or "")[:255], "size": vsize(v), "refs": vrefs(v)}
                    for v in volumes[:2000]],
    }
    gib = 1024 ** 3
    text = json.dumps(summary)
    return ActionOutput(
        text,
        {"images_size": summary["images_size"], "volumes_size": summary["volumes_size"],
         "reclaimable": summary["images_reclaimable"] + summary["volumes_reclaimable"]
                        + summary["build_cache_reclaimable"],
         "summary": f"images {summary['images_size'] / gib:.1f} GiB, volumes {summary['volumes_size'] / gib:.1f} GiB"},
    )


def _image_remove(params: dict, _config: AgentConfig) -> str:
    image = str(params.get("image") or "")
    if not (_IMAGE_ID.match(image) or _SAFE_IMAGE.match(image)):
        raise ValueError(f"Invalid image: {image!r}")
    # No force: an image a container still uses is refused by the engine,
    # which is the point — removing it would strand that container.
    deleted = _engine().delete(f"/images/{image}") or []
    collector.request_docker_recheck()
    gone = [d.get("Deleted") or d.get("Untagged") for d in deleted if isinstance(d, dict)]
    return ActionOutput(f"removed {image}: {len(gone)} layer(s)/tag(s)", {"removed": True})


def _prune_result(kind: str, body: dict, key: str) -> ActionOutput:
    removed = [x if isinstance(x, str) else (x.get("Deleted") or x.get("Untagged") or "")
               for x in (body or {}).get(key) or []]
    freed = int((body or {}).get("SpaceReclaimed") or 0)
    collector.request_docker_recheck()
    return ActionOutput(f"pruned {len(removed)} {kind}, freed {freed} bytes",
                        {"removed": len(removed), "space_reclaimed": freed})


def _image_prune(params: dict, _config: AgentConfig) -> str:
    """Dangling images only, or (``all``) every image no container uses."""
    filters = {"dangling": ["false"]} if _flag(params, "all") else {"dangling": ["true"]}
    body = _engine().post("/images/prune", query={"filters": json.dumps(filters)}, timeout=600)
    return _prune_result("images", body, "ImagesDeleted")


def _volume_remove(params: dict, _config: AgentConfig) -> str:
    name = _object_name(params, "volume", "volume name")
    # The engine refuses a volume a container (even a stopped one) still uses.
    _engine().delete(f"/volumes/{name}")
    collector.request_docker_recheck()
    return ActionOutput(f"removed volume {name}", {"removed": True})


def _volume_prune(params: dict, _config: AgentConfig) -> str:
    """Every volume no container uses — named ones too (``all``), which is what
    an operator looking at a list of unused volumes expects; without it newer
    engines prune anonymous volumes only."""
    filters = {"all": ["true"]} if _flag(params, "all") else {}
    query = {"filters": json.dumps(filters)} if filters else None
    body = _engine().post("/volumes/prune", query=query, timeout=600)
    return _prune_result("volumes", body, "VolumesDeleted")


def _network_remove(params: dict, _config: AgentConfig) -> str:
    name = _object_name(params, "network", "network name")
    if name in _BUILTIN_NETWORKS:
        raise ValueError(f"{name} is the engine's own network and cannot be removed")
    _engine().delete(f"/networks/{name}")
    collector.request_docker_recheck()
    return ActionOutput(f"removed network {name}", {"removed": True})


def _network_prune(params: dict, _config: AgentConfig) -> str:
    body = _engine().post("/networks/prune", timeout=120)
    return _prune_result("networks", body, "NetworksDeleted")
