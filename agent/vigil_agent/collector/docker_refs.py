"""The Docker recheck flag and the image-reference helpers behind update detection.

``_docker_recheck_requested`` is process-wide state — set by any action that
changes local Docker state, consumed by the monitor loop — so it lives once
here. The reference helpers normalize what ``RepoDigests``/``Config.Image`` say
into the ``<namespace>/<repo>:<tag>`` form the registry API wants; they are used
by both this module's digest check and the outdated-image metric.
"""

import requests

from ._common import logger

# Sentinel returned by _get_registry_digest when the registry answered 304
# Not Modified to our If-None-Match — the tag still points at a digest we
# already have locally, so the container is up to date by definition.
_DIGEST_UNCHANGED = "__unchanged__"


def _parse_docker_hub_ref(image_str: str) -> tuple[str, str] | None:
    """Parse a container image string into (docker_hub_name, tag).

    Returns None for non-Docker Hub images (private registries, ghcr.io, etc.).
    Only strings without a registry prefix (i.e. no dot before the first slash,
    or no slash at all) are treated as Docker Hub.
    """
    # Strip digest suffix if present (e.g. nginx@sha256:abc → nginx)
    image_str = image_str.split("@")[0]

    # Split off tag
    if ":" in image_str.rsplit("/", 1)[-1]:
        name_part, tag = image_str.rsplit(":", 1)
    else:
        name_part, tag = image_str, "latest"

    # Detect a registry prefix: first component contains a dot or colon → not Docker Hub
    first_component = name_part.split("/")[0]
    if "." in first_component or ":" in first_component or first_component == "localhost":
        return None

    # No slash → official image (library namespace)
    if "/" not in name_part:
        return f"library/{name_part}", tag

    return name_part, tag


# Sentinel returned by _get_registry_digest when the registry answered 304
# Not Modified to our If-None-Match — the tag still points at a digest we
# already have locally, so the container is up to date by definition.
# is what lets an outdated-image alert resolve minutes after remediation.
_docker_recheck_requested = False


def request_docker_recheck() -> None:
    """Flag that Docker update metrics should be refreshed on the next pass."""
    global _docker_recheck_requested
    _docker_recheck_requested = True


def consume_docker_recheck() -> bool:
    """Return whether a re-check was requested, clearing the flag."""
    global _docker_recheck_requested
    requested = _docker_recheck_requested
    _docker_recheck_requested = False
    return requested


def _normalize_hub_repo(name: str) -> str:
    """Normalize a RepoDigests repo name to Docker Hub's <namespace>/<repo> form."""
    for prefix in ("docker.io/", "registry-1.docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if "/" not in name:
        name = f"library/{name}"
    return name


def _local_hub_digests(repo_digests: list, hub_name: str) -> list[str]:
    """Digests the local image is known by for *hub_name* on Docker Hub.

    RepoDigests can hold entries for several repos when an image has been
    retagged or pulled through a mirror. Comparing against the wrong repo's
    digest makes a current container look permanently outdated — and every
    "update" pull that follows burns real (counted) pull quota without
    changing anything. Watchtower compares against every digest the local
    image is known by; do the same, preferring repo-matched entries.
    """
    matched: list[str] = []
    all_digests: list[str] = []
    for entry in repo_digests:
        if "@" not in entry:
            continue
        repo, digest = entry.rsplit("@", 1)
        all_digests.append(digest)
        if _normalize_hub_repo(repo) == hub_name:
            matched.append(digest)
    return matched or all_digests


def _get_registry_digest(hub_name: str, tag: str, local_digests: list[str] | None = None) -> str | None:
    """Return the current manifest digest for hub_name:tag from Docker Hub.

    Returns _DIGEST_UNCHANGED when the registry confirms via If-None-Match
    that the tag still resolves to a digest we already hold, or None on error.
    """
    try:
        token_resp = requests.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": f"repository:{hub_name}:pull"},
            timeout=10,
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("token")
        if not token:
            return None

        # HEAD, not GET: Docker Hub counts manifest *GET* requests against
        # the anonymous pull limit (100 / 6h / IP), but HEAD requests are
        # free. A GET here on every container every few minutes silently
        # exhausts the host's pull budget and breaks real `docker pull`s.
        # HEAD still returns the Docker-Content-Digest header we need.
        #
        # Accept lists the manifest-list / OCI-index types first so a
        # multi-arch image returns its *index* digest — matching what
        # Docker stores in RepoDigests — instead of a single-arch digest
        # that would always look "outdated".
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": ", ".join([
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
            ]),
        }
        # If-None-Match with the digests we already hold — the registry's
        # manifest ETag is the digest, so a 304 proves "no change" without
        # the manifest ever being served. This is watchtower's check-only
        # pattern and can never be metered as a pull.
        if local_digests:
            headers["If-None-Match"] = ", ".join(f'"{d}"' for d in local_digests)

        resp = requests.head(
            f"https://registry-1.docker.io/v2/{hub_name}/manifests/{tag}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 304:
            return _DIGEST_UNCHANGED
        resp.raise_for_status()
        return resp.headers.get("Docker-Content-Digest")
    except Exception as exc:
        logger.debug("Registry digest check failed for %s:%s — %s", hub_name, tag, exc)
        return None


