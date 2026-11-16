"""Service actions: systemd start/stop/restart/reload/enable/disable and check.

Moved out of executor.py (which was 1,664 lines). Handlers that go through the
``executor`` helpers tests patch by name — ``_run`` and nothing else here —
call them as ``ex.<name>`` so a test that patches
``vigil_agent.executor._run`` still reaches them; executor re-exports every
name defined here. ``_systemctl_query`` answers ``is-active`` / ``is-enabled``
without raising, since both exit non-zero for a perfectly normal negative
answer, and the mutating handlers report the resulting state from the query
helpers.
"""

from __future__ import annotations

import subprocess

from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _validate_name
from ..procenv import clean_env


def _systemctl_query(verb: str, name: str) -> str:
    """``systemctl is-active`` / ``is-enabled`` answer, without raising.

    Both exit non-zero for a perfectly normal "inactive" / "disabled" answer,
    so ``_run`` (which raises on non-zero) is the wrong tool here.
    """
    try:
        result = subprocess.run(
            ["systemctl", verb, name],
            capture_output=True, text=True, timeout=30, shell=False,
            env=clean_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().lower()


def _service_is_active(name: str) -> bool:
    return _systemctl_query("is-active", name) == "active"


def _service_is_enabled(name: str) -> bool:
    return _systemctl_query("is-enabled", name) == "enabled"


def _restart_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "restart", name]), {"active": _service_is_active(name)})


def _start_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "start", name]), {"active": _service_is_active(name)})


def _stop_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "stop", name]), {"active": _service_is_active(name)})


def _reload_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "reload", name]), {"active": _service_is_active(name)})


def _enable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "enable", name]), {"enabled": _service_is_enabled(name)})


def _disable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(ex._run(["systemctl", "disable", name]), {"enabled": _service_is_enabled(name)})


def _check_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    expect = str(params.get("expect", "")).lower()

    # systemctl is-active returns 0 for active, 3 for inactive — don't
    # raise on non-zero since "inactive" is a valid informational result.
    result = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True, text=True, timeout=30, shell=False,
        env=clean_env(),
    )
    actual = result.stdout.strip().lower()
    is_running = actual == "active"
    status_str = "running" if is_running else "stopped"

    if expect in ("running", "stopped") and status_str != expect:
        raise RuntimeError(
            f"Service {name} is {status_str}, expected {expect}"
        )

    return ActionOutput(
        f"Service {name}: {status_str} (systemctl: {actual})",
        {"active": is_running, "state": actual},
    )
