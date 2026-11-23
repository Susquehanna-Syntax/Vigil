"""The container inventory shipped with every check-in.

One row per container, running or not: the compact port map, a one-shot stats
read for running ones, and the restart policy / image digest the daemon only
reveals on the per-container inspect. Every read is best-effort — a container
that vanishes mid-pass simply loses those fields, never the whole inventory.
"""

from ._common import logger
from .docker_engine import _DOCKER_SOCKET, _docker_api_get, _engine_available


def _docker_ports(ports: list) -> list:
    """Compact the /containers/json Ports array to {private, public, type}."""
    compact = []
    for p in ports:
        if not isinstance(p, dict):
            continue
        entry = {"private": p.get("PrivatePort"), "type": p.get("Type", "tcp")}
        if p.get("PublicPort"):
            entry["public"] = p.get("PublicPort")
        compact.append(entry)
    return compact


def _docker_container_stats(container_id: str) -> dict:
    """One-shot CPU/mem stats for a running container.

    Best-effort: returns an empty dict when the stats endpoint is
    unavailable or the response is missing the fields we need. CPU percent
    mirrors ``docker stats`` (cpu delta / system delta * online cpus). Memory
    subtracts page cache so the figure tracks the container's real working set.
    """
    stats = _docker_api_get(f"/containers/{container_id}/stats?stream=false")
    if not isinstance(stats, dict):
        return {}
    out: dict = {}

    cpu = stats.get("cpu_stats") or {}
    precpu = stats.get("precpu_stats") or {}
    cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage")
    precpu_total = (precpu.get("cpu_usage") or {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    presystem = precpu.get("system_cpu_usage")
    if None not in (cpu_total, precpu_total, system, presystem):
        cpu_delta = cpu_total - precpu_total
        system_delta = system - presystem
        ncpu = cpu.get("online_cpus") or len(
            (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
        ) or 1
        if system_delta > 0 and cpu_delta >= 0:
            out["cpu_percent"] = round((cpu_delta / system_delta) * ncpu * 100.0, 2)

    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    limit = mem.get("limit")
    mem_detail = mem.get("stats") or {}
    cache = mem_detail.get("inactive_file") or mem_detail.get("cache") or 0
    if usage is not None:
        real = max(usage - cache, 0)
        out["mem_usage_bytes"] = real
        if limit:
            out["mem_limit_bytes"] = limit
            out["mem_percent"] = round((real / limit) * 100.0, 2)

    return out


def _container_detail(cid: str, image_id: str, digests: dict) -> dict:
    """Restart policy and the image's repo digest — best-effort, one inspect
    per container and one per image (cached across the snapshot)."""
    out: dict = {}
    try:
        spec = _docker_api_get(f"/containers/{cid}/json") if cid else None
        if isinstance(spec, dict):
            out["restart_policy"] = str(((spec.get("HostConfig") or {}).get("RestartPolicy")
                                         or {}).get("Name") or "")[:40]
        if image_id and image_id not in digests:
            image = _docker_api_get(f"/images/{image_id}/json")
            repo_digests = (image or {}).get("RepoDigests") or []
            digests[image_id] = str(repo_digests[0]) if repo_digests else ""
        if image_id:
            out["image_digest"] = digests[image_id][:300]
    except Exception as exc:  # noqa: BLE001 — detail is optional
        logger.debug("container detail for %s failed: %s", cid[:12], exc)
    return out


def _container_networks(c: dict) -> list[dict]:
    nets = ((c.get("NetworkSettings") or {}).get("Networks") or {})
    return [{"name": str(name)[:255], "ip": str((n or {}).get("IPAddress") or "")[:64],
             "network_id": str((n or {}).get("NetworkID") or "")[:64]}
            for name, n in list(nets.items())[:16]]


def _container_mounts(c: dict) -> list[dict]:
    return [{"type": str(m.get("Type") or "")[:16], "name": str(m.get("Name") or "")[:255],
             "source": str(m.get("Source") or "")[:500], "destination": str(m.get("Destination") or "")[:500],
             "rw": bool(m.get("RW"))}
            for m in (c.get("Mounts") or [])[:32] if isinstance(m, dict)]



def collect_docker_containers() -> list[dict] | None:
    """Snapshot of Docker containers on this host for the checkin payload.

    Returns ``None`` when Docker is unavailable (no socket, permission
    denied, daemon down) so the server leaves the previous snapshot alone
    instead of wiping it on a transient hiccup. Returns ``[]`` only when the
    daemon is reachable and genuinely runs no containers. Each entry carries
    identity, compose stack/service (from labels), state, and best-effort
    CPU/mem stats for running containers.
    """
    if not _engine_available():
        return None
    try:
        containers = _docker_api_get("/containers/json?all=1")
    except PermissionError:
        logger.warning("Cannot access Docker socket %s — permission denied", _DOCKER_SOCKET)
        return None
    except Exception as exc:
        logger.debug("Docker container list failed: %s", exc)
        return None
    if not isinstance(containers, list):
        return None

    out: list[dict] = []
    digests: dict[str, str] = {}
    for c in containers:
        cid = c.get("Id", "")
        names = c.get("Names") or []
        name = names[0].lstrip("/") if names else (cid[:12] or "unknown")
        labels = c.get("Labels") or {}
        state = (c.get("State") or "").lower()
        entry = {
            "container_id": cid[:64],
            "name": name,
            "image": c.get("Image", ""),
            "state": state,
            "status": c.get("Status", ""),
            "stack": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
            "ports": _docker_ports(c.get("Ports") or []),
            # Stack inventory (M11): where compose keeps the stack, and the
            # hash compose uses to tell whether a container drifted.
            "image_id": c.get("ImageID", "")[:80],
            "config_files": labels.get("com.docker.compose.project.config_files", "")[:1000],
            "working_dir": labels.get("com.docker.compose.project.working_dir", "")[:500],
            "config_hash": labels.get("com.docker.compose.config-hash", "")[:80],
            # Edges for the Networks/Volumes tabs (and a topology view): which
            # networks the container sits on, which volumes it mounts.
            "networks": _container_networks(c),
            "mounts": _container_mounts(c),
        }
        entry.update(_container_detail(cid, c.get("ImageID", ""), digests))
        if state == "running" and cid:
            try:
                entry.update(_docker_container_stats(cid))
            except Exception as exc:
                logger.debug("Docker stats for %s failed: %s", name, exc)
        out.append(entry)
    return out
