"""Outdated-image metrics for running containers.

One pass over ``/containers/json`` plus a digest revalidation per distinct image
(kept in a per-scrape cache so twenty containers of one tag cost one registry
round-trip). The reference parsing and the Docker Hub exchange live in
``docker_refs``; this module only turns their answers into points.
"""

from ._common import _point, logger
from .docker_engine import _DOCKER_SOCKET, _docker_api_get, _engine_available
from .docker_refs import (
    _DIGEST_UNCHANGED,
    _get_registry_digest,
    _local_hub_digests,
    _normalize_hub_repo,
    _parse_docker_hub_ref,
)


def collect_docker_updates() -> list[dict]:
    """Check running Docker containers for outdated images.

    Returns metric dicts for the checkin payload. Skipped silently when Docker
    is unavailable (no socket, permission denied, daemon not running).
    Only Docker Hub public images are checked; private registries are skipped.
    """
    if not _engine_available():
        return []

    try:
        containers = _docker_api_get("/containers/json")
    except PermissionError:
        logger.warning("Cannot access Docker socket %s — permission denied", _DOCKER_SOCKET)
        return []
    except Exception as exc:
        logger.debug("Docker socket query failed: %s", exc)
        return []

    if not isinstance(containers, list):
        return []

    metrics: list[dict] = [_point("docker", "running_count", float(len(containers)))]

    # One registry query per unique image:tag pair
    digest_cache: dict[str, str | None] = {}
    ts = _now_iso()

    for container in containers:
        image_str = container.get("Image", "")
        ref = _parse_docker_hub_ref(image_str)
        if ref is None:
            logger.debug("Skipping non-Docker-Hub image: %s", image_str)
            continue

        hub_name, tag = ref
        container_name = (container.get("Names") or ["unknown"])[0].lstrip("/")
        image_id = container.get("ImageID", "")

        # Get local manifest digests from image inspect
        image_info = _docker_api_get(f"/images/{image_id}/json") if image_id else None
        repo_digests = (image_info or {}).get("RepoDigests", [])
        if not repo_digests:
            logger.debug("No RepoDigests for %s (%s) — locally built, skipping", container_name, image_str)
            continue
        local_digests = _local_hub_digests(repo_digests, hub_name)
        if not local_digests:
            logger.debug("No usable RepoDigests for %s (%s), skipping", container_name, image_str)
            continue

        # Registry digest (cached per image id + tag — the If-None-Match set
        # depends on the local image, so two containers only share a cache
        # entry when they run the exact same image)
        cache_key = f"{hub_name}:{tag}@{image_id}"
        if cache_key not in digest_cache:
            digest_cache[cache_key] = _get_registry_digest(hub_name, tag, local_digests)
        remote_digest = digest_cache[cache_key]

        if remote_digest is None:
            continue  # Registry unreachable — don't emit a metric this cycle

        up_to_date = remote_digest == _DIGEST_UNCHANGED or remote_digest in local_digests
        outdated = 0.0 if up_to_date else 1.0
        metrics.append({
            "category": "docker",
            "metric": "image_outdated",
            "value": outdated,
            "labels": {
                "container_name": container_name,
                "image": image_str,
                "local_digest": local_digests[0][:19],
                "remote_digest": "unchanged" if remote_digest == _DIGEST_UNCHANGED else remote_digest[:19],
            },
            "time": ts,
        })
        logger.debug(
            "Docker %s (%s): %s",
            container_name,
            image_str,
            "OUTDATED" if outdated else "up to date",
        )

    return metrics


