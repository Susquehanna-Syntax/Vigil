"""HTTPS client for Vigil server communication.

All requests verify TLS certificates. There is no option to disable this.
"""

import logging
import os
import platform
import socket

import requests

from .__version__ import __version__
from .config import AgentConfig
from .features import FEATURES

logger = logging.getLogger("vigil.client")

_TIMEOUT = (10, 30)  # (connect, read) seconds


def _headers(config: AgentConfig) -> dict:
    return {"Authorization": f"Bearer {config.agent_token}"}


def _system_info() -> dict:
    from . import collector  # local import: collector imports client

    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.release(),
        "machine_id": collector.machine_fingerprint(),
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
    reboot_required: bool | None = None,
    windows_updates: dict | None = None,
    software: dict | None = None,
    windows_update_list: list[dict] | None = None,
    container_engines: list[dict] | None = None,
) -> dict:
    """Send metrics and receive tasks. Returns the full server response."""
    payload = {
        **_system_info(),
        "vigil_version": __version__,
        "mode": config.mode,
        "metrics": metrics,
        "features": list(FEATURES),
        # What this agent will run, so the server can warn before a deploy to
        # a host whose allowlist refuses the task (M7). Empty is meaningful.
        "allowlist": sorted(config.allowlist),
        "allow_reprovision": bool(config.allow_reprovision),
    }
    # A managed agent left on the monitor-mode unit runs unprivileged and fails
    # every task; the server marks such a host as refusing them (QA-08).
    try:
        payload["runs_as_root"] = os.geteuid() == 0
    except AttributeError:      # Windows
        pass
    # The ingest distinguishes an absent key (agent too old to report it —
    # stored value left alone) from an explicit False, so the key is only
    # sent when the probe produced a value.
    if reboot_required is not None:
        payload["reboot_required"] = reboot_required
    # Same contract: absent means "this agent cannot count" — not Windows, no
    # backend, or the scan failed — and the server keeps what it already knew
    # rather than being told zero by a machine that cannot count.
    if windows_updates is not None:
        payload["windows_updates"] = windows_updates
    # The per-update list rides along only after a fresh scan; absent means
    # "nothing new", and the server keeps the rows it has.
    if windows_update_list is not None:
        payload["windows_update_list"] = windows_update_list
    # Docker / Podman engines this host runs (M11); absent = none found, and
    # the server keeps what it had.
    if container_engines is not None:
        payload["container_engines"] = container_engines
    if config.tags:
        payload["tags"] = list(config.tags)
    if inventory:
        payload["inventory"] = inventory
    # None → Docker unavailable, omit the key so the server keeps the prior
    # snapshot. An empty list is meaningful ("no containers now") and is sent.
    if docker_containers is not None:
        payload["docker_containers"] = docker_containers
    # Same contract: an absent key means "nothing new to store", so the server
    # keeps the software list it already has. Only a fresh payload is sent.
    if software is not None:
        payload["software"] = software
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


def report_result(
    config: AgentConfig, task_id: str, state: str, output: str, steps=None
) -> dict:
    """Report task execution result to the server."""
    payload = {
        "task_id": task_id,
        "state": state,
        "output": _cap_output(output),
    }
    if steps is not None:
        payload["steps"] = steps
    url = f"{config.server_url}/api/v1/tasks/result/"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def post_log_lines(config: AgentConfig, session: str, lines: list[str]) -> bool:
    """Ship new log lines for a live tail (M11); True while the viewer is
    still watching. Any failure stops the tail — it is a convenience, and an
    agent must never hammer a server that is not answering."""
    url = f"{config.server_url}/api/v1/agent/log-tail/{session}/"
    try:
        resp = requests.post(url, json={"lines": lines}, headers=_headers(config), timeout=10)
        resp.raise_for_status()
        return bool(resp.json().get("continue"))
    except Exception:  # noqa: BLE001 — see above
        return False


def fetch_stack_env(config: AgentConfig, ticket: str) -> str:
    """Redeem a deploy's one-time env ticket (M11) for the .env text."""
    url = f"{config.server_url}/api/v1/agent/stack-env/{ticket}/"
    resp = requests.get(url, headers=_headers(config), timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"the server would not hand over this deploy's .env "
                           f"({resp.status_code}) — the ticket is used or expired; deploy again")
    return str(resp.json().get("env") or "")


#: A Git stack's packed source folder (the server caps it at 20 MB).
MAX_STACK_SOURCE_BYTES = 20 * 1024 * 1024


def fetch_stack_source(config: AgentConfig, ticket: str) -> bytes:
    """Redeem a deploy's one-time source ticket (2026.14.1) for the packed
    folder of a Git stack. The caller checks it against the signed sha256."""
    url = f"{config.server_url}/api/v1/agent/stack-source/{ticket}/"
    with requests.get(url, headers=_headers(config), timeout=_TIMEOUT, stream=True) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"the server would not hand over this deploy's source "
                               f"({resp.status_code}) — the ticket is used or expired; deploy again")
        body = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > MAX_STACK_SOURCE_BYTES:
                raise RuntimeError("the stack's source is larger than an agent accepts")
    return bytes(body)


def post_stack_read(config: AgentConfig, ticket: str, payload: dict) -> None:
    """Hand an adopted stack's files to the server (M11) — over the agent's
    own connection, never in the task result, because .env holds secrets."""
    url = f"{config.server_url}/api/v1/agent/stack-adopt/{ticket}/"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"the server refused the adoption ({resp.status_code}) — "
                           f"the ticket is used or expired; adopt again")


def fetch_registry_auth(config: AgentConfig, registry: str) -> dict | None:
    """The private registry login the server holds for this host (M11), or
    None — a public pull needs none, and any failure means pull anonymously."""
    url = f"{config.server_url}/api/v1/agent/registry-auth/"
    try:
        resp = requests.get(url, params={"registry": registry}, headers=_headers(config),
                            timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    return body if isinstance(body, dict) and body.get("username") else None
