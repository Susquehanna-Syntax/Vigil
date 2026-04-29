"""HTTPS client for Vigil server communication.

All requests verify TLS certificates. There is no option to disable this.
"""

import logging
import platform
import socket

import requests

from .config import AgentConfig

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


def checkin(config: AgentConfig, metrics: list[dict], inventory: dict | None = None) -> dict:
    """Send metrics and receive tasks. Returns the full server response."""
    payload = {
        **_system_info(),
        "metrics": metrics,
    }
    if config.tags:
        payload["tags"] = list(config.tags)
    if inventory:
        payload["inventory"] = inventory
    url = f"{config.server_url}/api/v1/checkin"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def report_result(config: AgentConfig, task_id: str, state: str, output: str) -> dict:
    """Report task execution result to the server."""
    payload = {
        "task_id": task_id,
        "state": state,
        "output": output[:10_000],  # Cap output size
    }
    url = f"{config.server_url}/api/v1/tasks/result/"
    resp = requests.post(url, json=payload, headers=_headers(config), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
