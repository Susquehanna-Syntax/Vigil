"""Task execution engine with allowlist enforcement and input validation.

Security invariants:
 - monitor-mode agents never execute anything
 - managed-mode agents only execute actions present in their local allowlist
 - full_control agents execute any known action (not arbitrary shell commands)
 - all subprocess calls use list arguments (never shell=True)
 - all user-supplied parameters are validated against strict patterns before use
 - execute_script resolves paths and ensures they stay within scripts_dir
 - run_command is restricted to full_control mode only (defense-in-depth)
 - file operations protect sensitive system files and pseudo-filesystems
"""

from __future__ import annotations

import hashlib  # noqa: F401 - tests patch executor.hashlib
import json  # noqa: F401 - tests patch executor.json
import logging
import os  # noqa: F401 - tests patch executor.os (platform branch, uname, chmod)
import re
import shlex  # noqa: F401 - tests patch executor.shlex
import shutil
import stat
import subprocess  # tests patch executor.subprocess.run / .Popen
import sys  # noqa: F401 - tests patch executor.sys (platform, argv, frozen)
import tempfile  # noqa: F401 - tests patch executor.tempfile
import threading  # noqa: F401 - tests patch executor.threading
import time  # noqa: F401 - tests patch executor.time
from pathlib import Path

# Kept as executor attributes: tests patch these module objects and the moved
# handlers reach them through `ex.<name>`, so they must stay reachable here.
from . import (
    collector,  # noqa: F401 — tests patch executor.collector
    firewall,  # noqa: F401 - tests patch executor.firewall.detect
    scripthash,  # noqa: F401 - tests patch executor.scripthash.script_hash
    software,  # noqa: F401 - tests patch executor.software.collect_now
)
from .config import AgentConfig
from .deferral import RebootDeferral  # noqa: F401 - tests patch executor.RebootDeferral
from .pkg_manager import detect as detect_pkg_manager  # noqa: F401 - tests patch executor.detect_pkg_manager
from .procenv import clean_env

logger = logging.getLogger("vigil.executor")

# ── Validation patterns ─────────────────────────────────────────────────────
# No shell metacharacters in any of these.

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@._:-]{0,254}$")
_SAFE_SCRIPT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SAFE_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SAFE_GROUP = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SAFE_HOSTNAME = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
_SAFE_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}$")
_SAFE_CRON_SCHEDULE = re.compile(r"^[@a-zA-Z0-9*/, -]{1,64}$")
_OCTAL_MODE = re.compile(r"^0?[0-7]{3,4}$")

_EXEC_TIMEOUT = 120  # seconds

# ── File-operation safeguards ────────────────────────────────────────────────

_SENSITIVE_WRITE_PATHS = frozenset({
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/master.passwd",
})

_UNDELETABLE_PATHS = frozenset({
    "/", "/etc", "/usr", "/var", "/bin", "/sbin",
    "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev",
    "/home", "/root",
})

_BLOCKED_PREFIXES = ("/proc/", "/sys/", "/dev/")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate_name(value: str, label: str) -> str:
    """Validate a service/container name against the safe pattern."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _validate_path(path_str: str, label: str = "path") -> Path:
    """Resolve and validate a filesystem path. Rejects pseudo-filesystems."""
    if not path_str:
        raise ValueError(f"{label} is required")
    path = Path(path_str).resolve()
    path_s = str(path)
    for prefix in _BLOCKED_PREFIXES:
        if path_s.startswith(prefix):
            raise ValueError(f"{label} in blocked pseudo-filesystem: {path_s}")
    return path


def _validate_write_path(path_str: str, config: AgentConfig) -> Path:
    """Validate a path for write operations. Rejects sensitive files."""
    path = _validate_path(path_str, "path")
    path_s = str(path)
    if path_s in _SENSITIVE_WRITE_PATHS:
        raise ValueError(f"Refusing to write sensitive file: {path_s}")
    # Protect the agent's own data directory
    data_dir_s = str(config.data_dir.resolve())
    if path_s == data_dir_s or path_s.startswith(data_dir_s + "/"):
        raise ValueError(f"Refusing to write inside agent data directory")
    return path


def _validate_delete_path(path_str: str, recursive: bool) -> Path:
    """Validate a path for deletion. Rejects critical system paths."""
    path = _validate_path(path_str, "path")
    path_s = str(path)
    if path_s in _UNDELETABLE_PATHS:
        raise ValueError(f"Refusing to delete protected path: {path_s}")
    if not path.exists():
        raise ValueError(f"Path does not exist: {path_s}")
    if path.is_dir() and not recursive:
        raise ValueError(
            f"Path is a directory; set recursive to true to delete: {path_s}"
        )
    return path


def _parse_octal_mode(mode_str: str) -> int:
    """Parse a mode string like '0644' into an integer."""
    mode_str = str(mode_str).strip()
    if not _OCTAL_MODE.match(mode_str):
        raise ValueError(f"Invalid file mode: {mode_str!r}")
    return int(mode_str, 8)


def _chown(path: Path, owner: str, group: str) -> None:
    """Change ownership of a path."""
    if owner and not _SAFE_USERNAME.match(owner):
        raise ValueError(f"Invalid owner: {owner!r}")
    if group and not _SAFE_GROUP.match(group):
        raise ValueError(f"Invalid group: {group!r}")
    shutil.chown(path, user=owner or None, group=group or None)


def _run(
    cmd: list[str],
    timeout: int = _EXEC_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a command and return combined stdout+stderr. Never uses shell."""
    logger.info("Executing: %s", cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        env=clean_env(extra_env),
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Command exited {result.returncode}: {output}")
    return output


# ═════════════════════════════════════════════════════════════════════════════
# ACTION HANDLERS
# ═════════════════════════════════════════════════════════════════════════════


class ActionOutput(str):
    """A handler's text plus its declared outputs.

    A str subclass so every existing caller that treats the result as text keeps working;
    the runtime reads ``.data`` to expose ``steps.<id>.result.<field>``.
    """

    data: dict

    def __new__(cls, text: str, data: dict | None = None):
        obj = super().__new__(cls, text)
        obj.data = dict(data or {})
        return obj


# ── Container engine helpers ────────────────────────────────────────────────


def _engine():
    """The container engine every container action talks to (M11) — the first
    answering socket, rootful before rootless. Tests patch this."""
    from . import engine

    client = engine.default_client()
    if client is None:
        raise RuntimeError("No container engine found (Docker or Podman socket)")
    return client


def _compose_cmd() -> list[str]:
    """``docker compose``, or ``podman compose`` on a host with no docker CLI."""
    import shutil

    return ["docker", "compose"] if shutil.which("docker") or not shutil.which("podman") \
        else ["podman", "compose"]


def _compose_env() -> dict[str, str]:
    """Point compose at the engine the client uses, whichever socket that is."""
    return {"DOCKER_HOST": f"unix://{_engine().socket_path}"}


def _container_running(name: str) -> bool:
    try:
        state = (_engine().get(f"/containers/{name}/json") or {}).get("State") or {}
    except Exception:  # noqa: BLE001 — unknown is "not running"
        return False
    return bool(state.get("Running"))


# Handlers split into modules (re-exported so executor.<name> keeps working).
from .actions.accounts import (  # noqa: E402,F401
    _add_user_to_group,
    _create_cron_job,
    _create_user,
    _delete_cron_job,
    _delete_user,
)
from .actions.alerts import (  # noqa: E402,F401
    MESSAGE_CAP as _ALERT_MESSAGE_CAP,
)
from .actions.alerts import (  # noqa: E402,F401
    SEVERITIES as _ALERT_SEVERITIES,
)
from .actions.alerts import (
    _raise_alert,
    _resolve_alert,
    _send_notification,
)
from .actions.apps import (
    _app_ensure,
    _app_install,
    _app_install_custom,
    _app_inventory,
    _app_pin,
    _app_uninstall,
    _app_upgrade,
)
from .actions.containers import (  # noqa: E402,F401
    _COMPOSE_PROJECT_LABEL,
    ROLLBACK_OVERRIDE,
    _check_docker_updates,
    _clear_docker_logs,
    _container_image_id,
    _container_rollback,
    _docker_compose_down,
    _docker_compose_up,
    _docker_inspect,
    _image_id,
    _pull,
    _pull_image,
    _recreate_body,
    _recreate_container,
    _registry_auth,
    _remove_container,
    _restart_container,
    _stack_compose_args,
    _stack_restart,
    _stack_update,
    _start_container,
    _stop_container,
    _update_container,
    registry_of,
)
from .actions.docker_resources import (  # noqa: E402,F401
    _docker_disk_usage,
    _image_prune,
    _image_remove,
    _network_prune,
    _network_remove,
    _volume_prune,
    _volume_remove,
)
from .actions.files import (  # noqa: E402,F401
    _copy_file,
    _create_directory,
    _delete_path,
    _move_file,
    _set_permissions,
    _write_file,
)
from .actions.firewall_rules import (  # noqa: E402,F401
    _add_firewall_rule,
    _disable_firewall,
    _enable_firewall,
    _list_firewall_rules,
    _remove_firewall_rule,
    _set_firewall_policy,
)
from .actions.hunts import (
    _hunt_content,
    _hunt_file,
    _hunt_package,
    _hunt_port,
    _hunt_process,
    _hunt_registry,
    _hunt_service,
)
from .actions.jackil import (  # noqa: E402,F401
    _jackil_note,
    _jackil_resolve,
    _jackil_ticket,
)
from .actions.logs import _container_logs  # noqa: E402,F401
from .actions.packages import (  # noqa: E402,F401
    _assert_initramfs_clean,
    _clear_temp_files,
    _install_package,
    _remove_package,
    _run_package_updates,
    _update_package,
)
from .actions.power import (  # noqa: E402,F401
    _hibernate_now,
    _set_display_timeout,
    _set_hibernate,
    _set_lid_action,
    _set_power_plan,
    _set_sleep_timeout,
    _sleep_now,
)
from .actions.selfupdate import (  # noqa: E402,F401
    _looks_like_zip,
    _reprovision_cleanup,
    _reprovision_commit,
    _reprovision_preflight,
    _reprovision_stage,
    _sha256_file,
    _stage_onedir_update,
    _sync_systemd_proxy,
    _update_agent,
)
from .actions.services import (  # noqa: E402,F401
    _check_service,
    _disable_service,
    _enable_service,
    _reload_service,
    _restart_service,
    _service_is_active,
    _service_is_enabled,
    _start_service,
    _stop_service,
    _systemctl_query,
)
from .actions.stacks import _stack_deploy, _stack_read, _stack_remove  # noqa: E402,F401
from .actions.system import (  # noqa: E402,F401
    _execute_inline_script,
    _execute_script,
    _input_env,
    _notify_user,
    _reboot,
    _run_command,
    _sanitize_notify_message,
    _set_hostname,
)
from .actions.tags_scans import (  # noqa: E402,F401
    _TRIVY_RESULT_BULK,
    _TRIVY_SCAN_TIMEOUT,
    _TRIVY_SCOPE_PATTERN,
    _TRIVY_SKIP_DIRS,
    _TRIVY_SUBPROCESS_TIMEOUT,
    _TRIVY_VULN_FIELDS,
    TRIVY_GZIP_MARKER,
    _add_tag,
    _condense_trivy_report,
    _count_trivy_vulnerabilities,
    _pack_report,
    _remove_tag,
    _request_nessus_scan,
    _request_network_scan,
    _run_trivy_scan,
    _slim_report,
    _tag_names,
    _trivy_db_update,
)
from .actions.winupdate import _windows_update_install, _windows_update_scan  # noqa: E402,F401

# ═════════════════════════════════════════════════════════════════════════════
# DISPATCH TABLE
# ═════════════════════════════════════════════════════════════════════════════

_HANDLERS: dict[str, callable] = {
    # Service management
    "restart_service": _restart_service,
    "start_service": _start_service,
    "stop_service": _stop_service,
    "reload_service": _reload_service,
    "enable_service": _enable_service,
    "disable_service": _disable_service,
    "check_service": _check_service,
    # Container management
    "restart_container": _restart_container,
    "stop_container": _stop_container,
    "start_container": _start_container,
    "pull_image": _pull_image,
    "recreate_container": _recreate_container,
    "update_container": _update_container,
    "check_docker_updates": _check_docker_updates,
    "add_tag": _add_tag,
    "remove_tag": _remove_tag,
    "raise_alert": _raise_alert,
    "resolve_alert": _resolve_alert,
    "send_notification": _send_notification,
    "jackil_ticket": _jackil_ticket,
    "jackil_note": _jackil_note,
    "jackil_resolve": _jackil_resolve,
    "request_nessus_scan": _request_nessus_scan,
    "request_network_scan": _request_network_scan,
    "run_trivy_scan": _run_trivy_scan,
    "trivy_db_update": _trivy_db_update,
    "hunt_file": _hunt_file,
    "hunt_package": _hunt_package,
    "hunt_process": _hunt_process,
    "hunt_port": _hunt_port,
    "hunt_service": _hunt_service,
    "hunt_registry": _hunt_registry,
    "hunt_content": _hunt_content,
    "remove_container": _remove_container,
    "docker_compose_up": _docker_compose_up,
    "docker_compose_down": _docker_compose_down,
    "stack_restart": _stack_restart,
    "stack_update": _stack_update,
    "container_logs": _container_logs,
    "stack_deploy": _stack_deploy,
    "stack_remove": _stack_remove,
    "stack_read": _stack_read,
    "container_rollback": _container_rollback,
    "docker_disk_usage": _docker_disk_usage,
    "image_remove": _image_remove,
    "image_prune": _image_prune,
    "volume_remove": _volume_remove,
    "volume_prune": _volume_prune,
    "network_remove": _network_remove,
    "network_prune": _network_prune,
    "clear_docker_logs": _clear_docker_logs,
    # File / directory operations
    "write_file": _write_file,
    "create_directory": _create_directory,
    "delete_path": _delete_path,
    "copy_file": _copy_file,
    "move_file": _move_file,
    "set_permissions": _set_permissions,
    # Package management
    "install_package": _install_package,
    "remove_package": _remove_package,
    "update_package": _update_package,
    "run_package_updates": _run_package_updates,
    # System
    "clear_temp_files": _clear_temp_files,
    "execute_script": _execute_script,
    "reboot": _reboot,
    "run_command": _run_command,
    "set_hostname": _set_hostname,
    # Power
    "set_sleep_timeout": _set_sleep_timeout,
    "set_display_timeout": _set_display_timeout,
    "set_hibernate": _set_hibernate,
    "set_power_plan": _set_power_plan,
    "set_lid_action": _set_lid_action,
    "sleep_now": _sleep_now,
    "hibernate_now": _hibernate_now,
    # Networking
    "add_firewall_rule": _add_firewall_rule,
    "remove_firewall_rule": _remove_firewall_rule,
    "list_firewall_rules": _list_firewall_rules,
    "set_firewall_policy": _set_firewall_policy,
    "enable_firewall": _enable_firewall,
    "disable_firewall": _disable_firewall,
    # Windows Update
    "windows_update_scan": _windows_update_scan,
    "windows_update_install": _windows_update_install,
    # Apps
    "app_inventory": _app_inventory,
    "app_install": _app_install,
    "app_upgrade": _app_upgrade,
    "app_uninstall": _app_uninstall,
    "app_pin": _app_pin,
    "app_install_custom": _app_install_custom,
    "app_ensure": _app_ensure,
    # User management
    "create_user": _create_user,
    "delete_user": _delete_user,
    "add_user_to_group": _add_user_to_group,
    # Cron
    "create_cron_job": _create_cron_job,
    "delete_cron_job": _delete_cron_job,
    # Self-management
    "update_agent": _update_agent,
    # Reprovisioning. The three destructive ones are gated on
    # allow_reprovision by AgentConfig.task_allowed, never by mode or the
    # allowlist — see config.REPROVISION_ACTIONS.
    "reprovision_preflight": _reprovision_preflight,
    "reprovision_stage": _reprovision_stage,
    "reprovision_commit": _reprovision_commit,
    "reprovision_cleanup": _reprovision_cleanup,
}


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════


def execute_action(
    action: str,
    params: dict,
    config: AgentConfig,
    *,
    timeout: int | None = None,
    inputs: dict | None = None,
) -> str:
    """Execute a single action after allowlist validation.

    This is the primary entry point used by both the legacy single-step path
    and the multi-step ``TaskRuntime``.  Each action is validated individually
    against the agent's local mode/allowlist — a compromised server cannot
    escalate privileges beyond what the agent config permits.

    Only ``execute_script`` receives task inputs (as ``VIGIL_INPUT_*``
    environment variables); every other action gets its values through
    already-resolved params.

    Returns output string.
    Raises ``ValueError`` for disallowed or unknown actions.
    Raises ``RuntimeError`` for execution failures.
    """
    if not config.task_allowed(action):
        raise ValueError(
            f"Action '{action}' is not allowed in mode '{config.mode}' "
            f"with current allowlist"
        )

    handler = _HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"Unknown action: {action!r}")

    # A step-level `timeout:` arrives here as a keyword argument. It used to
    # be accepted and then silently dropped — runtime.py documented the key,
    # the server (once it started sending it) shipped it, and a long build
    # still died at the 120s default with nothing to explain why.
    #
    # Handlers read their timeout from params, so fold it in there. An
    # explicit params.timeout wins: it is the more specific of the two.
    if timeout is not None and "timeout" not in params:
        params = {**params, "timeout": timeout}

    if action == "execute_script":
        return _execute_script(params, config, inputs=inputs)
    return handler(params, config)


# Backward-compatible alias — the agent's __main__.py calls this for
# single-action tasks.
execute_task = execute_action
