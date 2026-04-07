"""Task execution engine with allowlist enforcement and input validation.

Security invariants:
 - monitor-mode agents never execute anything
 - managed-mode agents only execute actions present in their local allowlist
 - full_control agents execute any known action (not arbitrary shell commands)
 - all subprocess calls use list arguments (never shell=True)
 - all user-supplied parameters are validated against strict patterns before use
 - execute_script resolves paths and ensures they stay within scripts_dir
"""

import logging
import re
import subprocess
from pathlib import Path

from .config import AgentConfig

logger = logging.getLogger("vigil.executor")

# Strict validation patterns — no shell metacharacters
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@._:-]{0,254}$")
_SAFE_SCRIPT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_EXEC_TIMEOUT = 120  # seconds


def _validate_name(value: str, label: str) -> str:
    """Validate a service/container name against the safe pattern."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _run(cmd: list[str], timeout: int = _EXEC_TIMEOUT) -> str:
    """Run a command and return combined stdout+stderr. Never uses shell."""
    logger.info("Executing: %s", cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Command exited {result.returncode}: {output}")
    return output


# ── Action handlers ──────────────────────────────────────────────────────────


def _restart_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("name", ""), "service name")
    return _run(["systemctl", "restart", name])


def _restart_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    return _run(["docker", "restart", name])


def _stop_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    return _run(["docker", "stop", name])


def _start_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    return _run(["docker", "start", name])


def _clear_temp_files(params: dict, _config: AgentConfig) -> str:
    days = int(params.get("older_than_days", 7))
    if days < 0:
        raise ValueError("older_than_days must be non-negative")
    return _run(["find", "/tmp", "-type", "f", "-mtime", f"+{days}", "-delete"])


def _clear_docker_logs(params: dict, _config: AgentConfig) -> str:
    container = params.get("container_name", "")
    if container:
        _validate_name(container, "container name")
    # Truncate log files via docker inspect + truncate
    if container:
        log_path = _run(
            ["docker", "inspect", "--format={{.LogPath}}", container]
        )
        if log_path and Path(log_path).exists():
            Path(log_path).write_text("")
            return f"Truncated log for {container}"
        return "No log file found"
    return "No container specified"


def _run_package_updates(params: dict, _config: AgentConfig) -> str:
    security_only = params.get("security_only", False)
    # Detect package manager
    for pm, cmd in [
        ("apt-get", ["apt-get", "update", "-qq"]),
        ("dnf", ["dnf", "check-update", "-q"]),
    ]:
        try:
            _run(["which", pm], timeout=5)
        except RuntimeError:
            continue
        _run(cmd, timeout=300)
        if pm == "apt-get":
            upgrade_cmd = ["apt-get", "upgrade", "-y", "-qq"]
            if security_only:
                upgrade_cmd = [
                    "apt-get", "upgrade", "-y", "-qq",
                    "-o", "Dir::Etc::SourceList=/etc/apt/sources.list",
                ]
            return _run(upgrade_cmd, timeout=600)
        elif pm == "dnf":
            upgrade_cmd = ["dnf", "update", "-y", "-q"]
            if security_only:
                upgrade_cmd.append("--security")
            return _run(upgrade_cmd, timeout=600)
    return "No supported package manager found"


def _execute_script(params: dict, config: AgentConfig) -> str:
    script_name = params.get("script_name", "")
    if not _SAFE_SCRIPT_NAME.match(script_name):
        raise ValueError(f"Invalid script name: {script_name!r}")

    scripts_dir = config.scripts_dir.resolve()
    script_path = (scripts_dir / script_name).resolve()

    # Path traversal protection: resolved path must be inside scripts_dir
    if not str(script_path).startswith(str(scripts_dir) + "/"):
        raise ValueError(f"Script path escapes scripts directory: {script_name!r}")

    if not script_path.is_file():
        raise ValueError(f"Script not found: {script_name}")

    # Refuse to run scripts that are writable by group/others
    import stat
    st = script_path.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(
            f"Script {script_name} is writable by group/others — refusing to execute. "
            f"Run: chmod go-w {script_path}"
        )

    return _run([str(script_path)])


def _reboot(params: dict, _config: AgentConfig) -> str:
    delay = int(params.get("delay_seconds", 0))
    if delay < 0:
        raise ValueError("delay_seconds must be non-negative")
    if delay == 0:
        return _run(["shutdown", "-r", "now"])
    minutes = max(1, delay // 60)
    return _run(["shutdown", "-r", f"+{minutes}"])


# ── Dispatch table ───────────────────────────────────────────────────────────

_HANDLERS: dict[str, callable] = {
    "restart_service": _restart_service,
    "restart_container": _restart_container,
    "stop_container": _stop_container,
    "start_container": _start_container,
    "clear_temp_files": _clear_temp_files,
    "clear_docker_logs": _clear_docker_logs,
    "run_package_updates": _run_package_updates,
    "execute_script": _execute_script,
    "reboot": _reboot,
}


def execute_task(action: str, params: dict, config: AgentConfig) -> str:
    """Execute a task action after allowlist validation. Returns output string.

    Raises ValueError for disallowed or unknown actions.
    Raises RuntimeError for execution failures.
    """
    if not config.task_allowed(action):
        raise ValueError(
            f"Action '{action}' is not allowed in mode '{config.mode}' "
            f"with current allowlist"
        )

    handler = _HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"Unknown action: {action!r}")

    return handler(params, config)
