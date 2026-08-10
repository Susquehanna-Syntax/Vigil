"""HTTPS client for Vigil server communication.

All requests verify TLS certificates. There is no option to disable this.
"""

import logging
import platform
import socket

import requests

from .config import AgentConfig
from .__version__ import __version__

logger = logging.getLogger("vigil.client")

_TIMEOUT = (10, 30)  # (connect, read) seconds


def _headers(config: AgentConfig) -> dict:
    return {"Authorization": f"Bearer {config.agent_token}"}


def _system_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.release(),
    }


def register(config: AgentConfig) -> dict:
    """Register this agent with the Vigil server. Returns {"id", "status"}."""
    payload = {
        "agent_token": config.agent_token,
        **_system_info(),
    }
    if config.tags:
        payload["tags"] = list(config.tags)
    url = f"{config.server_url}/api/v1/register"
    resp = requests.post(url, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    result = resp.json()
    logger.info("Registered with server: id=%s status=%s", result.get("id"), result.get("status"))
    return result


def checkin(
    config: AgentConfig,
    metrics: list[dict],
    inventory: dict | None = None,
    docker_containers: list[dict] | None = None,
) -> dict:
    """Send metrics and receive tasks. Returns the full server response."""
    payload = {
        **_system_info(),
        "vigil_version": __version__,
        "mode": config.mode,
        "metrics": metrics,
    }
    if config.tags:
        payload["tags"] = list(config.tags)
    if inventory:
        payload["inventory"] = inventory
    # None → Docker unavailable, omit the key so the server keeps the prior
    # snapshot. An empty list is meaningful ("no containers now") and is sent.
    if docker_containers is not None:
        payload["docker_containers"] = docker_containers
    url = f"{config.server_url}/api/v1/checkin"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


#: Ceiling on task output, in characters.
#:
#: Was 10,000, applied as a silent ``output[:10_000]``, which is why no Trivy
#: scan was ever ingested: a raw report is ~30 MB, so the server received an
#: unterminated fragment and could only report that it found no report at all.
#:
#: Then 1,000,000, which was sized from a single sample — a workstation with
#: 265 unique findings condensing to 243 KB. That was too small for real
#: servers: production hit it on three hosts at once, with condensed reports
#: of 2-10 MB.
#:
#: Reports are now deduplicated (2.1x) and gzipped (5.0x after base64), which
#: is what keeps this number small instead of chasing the largest host in
#: anyone's fleet. A host with 11,000 unique findings — far beyond the ones
#: that broke — packs to under 1 MB, so this leaves room to spare without
#: asking the server to buffer megabytes per request.
#:
#: Must stay below the server's DATA_UPLOAD_MAX_MEMORY_SIZE, or Django rejects
#: the whole POST with a 400 the agent cannot explain.
_MAX_OUTPUT = 2_000_000


def _cap_output(output: str | None) -> str:
    """Bound task output, saying so when the bound actually bites.

    Truncation used to be silent, so a result that arrived mangled was
    indistinguishable from one that arrived whole. Anything that reads this
    output — a human in run details, or the Trivy ingest path — can now tell
    the difference.
    """
    text = output or ""
    if len(text) <= _MAX_OUTPUT:
        return text
    dropped = len(text) - _MAX_OUTPUT
    return (f"{text[:_MAX_OUTPUT]}\n"
            f"[OUTPUT TRUNCATED — {dropped:,} of {len(text):,} characters "
            f"dropped at the agent's {_MAX_OUTPUT:,}-character cap]")


def report_result(config: AgentConfig, task_id: str, state: str, output: str) -> dict:
    """Report task execution result to the server."""
    payload = {
        "task_id": task_id,
        "state": state,
        "output": _cap_output(output),
    }
    url = f"{config.server_url}/api/v1/tasks/result/"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
