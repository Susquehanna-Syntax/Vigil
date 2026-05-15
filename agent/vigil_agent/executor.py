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

import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import AgentConfig
from .pkg_manager import detect as detect_pkg_manager

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


# ═════════════════════════════════════════════════════════════════════════════
# ACTION HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

# ── Service management ──────────────────────────────────────────────────────


def _restart_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "restart", name])


def _start_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "start", name])


def _stop_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "stop", name])


def _reload_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "reload", name])


def _enable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "enable", name])


def _disable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return _run(["systemctl", "disable", name])


def _check_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    expect = str(params.get("expect", "")).lower()

    # systemctl is-active returns 0 for active, 3 for inactive — don't
    # raise on non-zero since "inactive" is a valid informational result.
    result = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True, text=True, timeout=30, shell=False,
    )
    actual = result.stdout.strip().lower()
    is_running = actual == "active"
    status_str = "running" if is_running else "stopped"

    if expect in ("running", "stopped") and status_str != expect:
        raise RuntimeError(
            f"Service {name} is {status_str}, expected {expect}"
        )

    return f"Service {name}: {status_str} (systemctl: {actual})"


# ── Container management ────────────────────────────────────────────────────


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


def _pull_image(params: dict, _config: AgentConfig) -> str:
    image = params.get("image", "")
    if not _SAFE_IMAGE.match(image):
        raise ValueError(f"Invalid image name: {image!r}")
    return _run(["docker", "pull", image], timeout=600)


def _request_nessus_scan(_params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server records a Nessus scan request.

    No real work happens on the agent — Nessus scans the host's IP from
    the central scanner. The server inspects completed task params for
    this action and creates a ``VulnScan(state=REQUESTED)`` row, which
    the next ``sync_nessus_vulns`` cycle launches against Nessus.
    """
    return "Nessus scan requested — central scanner will pick it up"


def _remove_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("container_name", ""), "container name")
    return _run(["docker", "rm", "-f", name])


def _docker_compose_up(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = _validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")

    cmd = ["docker", "compose", "-f", str(path), "up", "-d"]

    services = params.get("services", "")
    if services:
        if isinstance(services, str):
            services = [s.strip() for s in services.split(",") if s.strip()]
        for svc in services:
            _validate_name(svc, "service name")
            cmd.append(svc)

    return _run(cmd, timeout=300)


def _docker_compose_down(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = _validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")
    return _run(["docker", "compose", "-f", str(path), "down"], timeout=120)


def _clear_docker_logs(params: dict, _config: AgentConfig) -> str:
    container = params.get("container_name", "")
    if not container:
        return "No container specified"
    _validate_name(container, "container name")
    log_path = _run(
        ["docker", "inspect", "--format={{.LogPath}}", container]
    )
    if log_path and Path(log_path).exists():
        Path(log_path).write_text("")
        return f"Truncated log for {container}"
    return "No log file found"


# ── File / directory operations ─────────────────────────────────────────────


def _write_file(params: dict, config: AgentConfig) -> str:
    path = _validate_write_path(params.get("path", ""), config)
    content = params.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    return f"Wrote {len(content)} bytes to {path}"


def _create_directory(params: dict, _config: AgentConfig) -> str:
    path = _validate_path(params.get("path", ""), "path")
    path.mkdir(parents=True, exist_ok=True)

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    owner = str(params.get("owner", ""))
    group = str(params.get("group", ""))
    if owner or group:
        _chown(path, owner, group)

    return f"Created directory {path}"


def _delete_path(params: dict, _config: AgentConfig) -> str:
    recursive = bool(params.get("recursive", False))
    path = _validate_delete_path(params.get("path", ""), recursive)

    if path.is_dir():
        shutil.rmtree(path)
        return f"Deleted directory {path} (recursive)"
    else:
        path.unlink()
        return f"Deleted {path}"


def _copy_file(params: dict, _config: AgentConfig) -> str:
    src = _validate_path(params.get("src", ""), "src")
    dest = _validate_path(params.get("dest", ""), "dest")
    if not src.exists():
        raise ValueError(f"Source not found: {src}")

    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return f"Copied {src} -> {dest}"


def _move_file(params: dict, _config: AgentConfig) -> str:
    src = _validate_path(params.get("src", ""), "src")
    dest = _validate_path(params.get("dest", ""), "dest")
    if not src.exists():
        raise ValueError(f"Source not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return f"Moved {src} -> {dest}"


def _set_permissions(params: dict, _config: AgentConfig) -> str:
    path = _validate_path(params.get("path", ""), "path")
    if not path.exists():
        raise ValueError(f"Path not found: {path}")

    mode = params.get("mode", "")
    if mode:
        os.chmod(path, _parse_octal_mode(str(mode)))

    owner = str(params.get("owner", ""))
    group = str(params.get("group", ""))
    if owner or group:
        _chown(path, owner, group)

    parts = []
    if mode:
        parts.append(f"mode={mode}")
    if owner:
        parts.append(f"owner={owner}")
    if group:
        parts.append(f"group={group}")
    return f"Set {', '.join(parts)} on {path}"


# ── Package management ──────────────────────────────────────────────────────


def _install_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    return pm.install(pkg_name)


def _remove_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    return pm.remove(pkg_name)


def _update_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    return pm.install(pkg_name)  # install upgrades if already present


def _run_package_updates(params: dict, _config: AgentConfig) -> str:
    security_only = params.get("security_only", False)
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")

    pm.refresh()

    if security_only:
        # Security-only upgrades only supported for apt and dnf
        if pm.name in ("apt", "apt-get"):
            return _run(
                ["apt-get", "upgrade", "-y", "-qq",
                 "-o", "Dir::Etc::SourceList=/etc/apt/sources.list"],
                timeout=600,
            )
        if pm.name == "dnf":
            return _run(
                ["dnf", "update", "-y", "-q", "--security"], timeout=600
            )
        logger.warning(
            "security_only not supported for %s, running full upgrade",
            pm.name,
        )

    return pm.upgrade_all()


# ── System ──────────────────────────────────────────────────────────────────


def _clear_temp_files(params: dict, _config: AgentConfig) -> str:
    days = int(params.get("older_than_days", 7))
    if days < 0:
        raise ValueError("older_than_days must be non-negative")
    return _run(
        ["find", "/tmp", "-type", "f", "-mtime", f"+{days}", "-delete"]
    )


def _execute_script(params: dict, config: AgentConfig) -> str:
    script_name = params.get("script_name", "")
    if not _SAFE_SCRIPT_NAME.match(script_name):
        raise ValueError(f"Invalid script name: {script_name!r}")

    scripts_dir = config.scripts_dir.resolve()
    script_path = (scripts_dir / script_name).resolve()

    # Path traversal protection
    if not str(script_path).startswith(str(scripts_dir) + "/"):
        raise ValueError(
            f"Script path escapes scripts directory: {script_name!r}"
        )

    if not script_path.is_file():
        raise ValueError(f"Script not found: {script_name}")

    st = script_path.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(
            f"Script {script_name} is writable by group/others — refusing "
            f"to execute. Run: chmod go-w {script_path}"
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


def _run_command(params: dict, config: AgentConfig) -> str:
    """Run an arbitrary command. full_control mode only (defense-in-depth)."""
    if config.mode != "full_control":
        raise ValueError("run_command is only available in full_control mode")

    command = params.get("command", "")
    if not command:
        raise ValueError("command is required")

    timeout = int(params.get("timeout", _EXEC_TIMEOUT))
    if timeout < 1 or timeout > 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")

    return _run(shlex.split(command), timeout=timeout)


def _set_hostname(params: dict, _config: AgentConfig) -> str:
    hostname = params.get("hostname", "")
    if not _SAFE_HOSTNAME.match(hostname):
        raise ValueError(f"Invalid hostname: {hostname!r}")
    return _run(["hostnamectl", "set-hostname", hostname])


# ── Networking ──────────────────────────────────────────────────────────────


def _add_firewall_rule(params: dict, _config: AgentConfig) -> str:
    port = int(params.get("port", 0))
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    protocol = str(params.get("protocol", "tcp")).lower()
    if protocol not in ("tcp", "udp"):
        raise ValueError(f"Protocol must be tcp or udp, got {protocol!r}")
    action = str(params.get("action", "allow")).lower()
    if action not in ("allow", "deny"):
        raise ValueError(f"Action must be allow or deny, got {action!r}")

    # Try ufw first, then firewall-cmd
    for tool in ("ufw", "firewall-cmd"):
        try:
            _run(["which", tool], timeout=5)
        except RuntimeError:
            continue

        if tool == "ufw":
            return _run(["ufw", action, f"{port}/{protocol}"])
        else:
            flag = f"--add-port={port}/{protocol}" if action == "allow" \
                else f"--remove-port={port}/{protocol}"
            return _run(["firewall-cmd", "--permanent", flag])

    raise RuntimeError("No supported firewall tool found (ufw or firewall-cmd)")


def _remove_firewall_rule(params: dict, _config: AgentConfig) -> str:
    port = int(params.get("port", 0))
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    protocol = str(params.get("protocol", "tcp")).lower()
    if protocol not in ("tcp", "udp"):
        raise ValueError(f"Protocol must be tcp or udp, got {protocol!r}")

    for tool in ("ufw", "firewall-cmd"):
        try:
            _run(["which", tool], timeout=5)
        except RuntimeError:
            continue

        if tool == "ufw":
            return _run(["ufw", "delete", "allow", f"{port}/{protocol}"])
        else:
            return _run([
                "firewall-cmd", "--permanent",
                f"--remove-port={port}/{protocol}",
            ])

    raise RuntimeError("No supported firewall tool found (ufw or firewall-cmd)")


# ── User management ────────────────────────────────────────────────────────


def _create_user(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")

    cmd = ["useradd"]

    groups = params.get("groups", "")
    if groups:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        for g in groups:
            if not _SAFE_GROUP.match(g):
                raise ValueError(f"Invalid group name: {g!r}")
        cmd.extend(["-G", ",".join(groups)])

    shell = params.get("shell", "")
    if shell:
        shell_path = _validate_path(shell, "shell")
        if not shell_path.exists():
            raise ValueError(f"Shell not found: {shell}")
        cmd.extend(["-s", str(shell_path)])

    cmd.append(username)
    return _run(cmd)


def _delete_user(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    cmd = ["userdel"]
    if params.get("remove_home", False):
        cmd.append("--remove")
    cmd.append(username)
    return _run(cmd)


def _add_user_to_group(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    group = params.get("group", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    if not _SAFE_GROUP.match(group):
        raise ValueError(f"Invalid group name: {group!r}")
    return _run(["usermod", "-aG", group, username])


# ── Cron management ─────────────────────────────────────────────────────────


def _create_cron_job(params: dict, _config: AgentConfig) -> str:
    schedule = params.get("schedule", "")
    if not _SAFE_CRON_SCHEDULE.match(schedule):
        raise ValueError(f"Invalid cron schedule: {schedule!r}")

    command = params.get("command", "")
    if not command:
        raise ValueError("command is required")
    if any(c in command for c in ("`", "$(")):
        raise ValueError("Command contains dangerous shell substitution")

    user = params.get("user", "root")
    if not _SAFE_USERNAME.match(user):
        raise ValueError(f"Invalid user: {user!r}")

    cron_line = f"{schedule} {command}"

    # Read existing crontab, append new entry
    try:
        existing = subprocess.run(
            ["crontab", "-u", user, "-l"],
            capture_output=True, text=True, timeout=10, shell=False,
        )
        current = existing.stdout if existing.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        current = ""

    new_crontab = current.rstrip("\n") + "\n" + cron_line + "\n"

    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set crontab: {proc.stderr.strip()}")

    return f"Added cron job for user {user}: {cron_line}"


def _delete_cron_job(params: dict, _config: AgentConfig) -> str:
    pattern = params.get("pattern", "")
    if not pattern:
        raise ValueError("pattern is required")

    user = params.get("user", "root")
    if not _SAFE_USERNAME.match(user):
        raise ValueError(f"Invalid user: {user!r}")

    result = subprocess.run(
        ["crontab", "-u", user, "-l"],
        capture_output=True, text=True, timeout=10, shell=False,
    )
    if result.returncode != 0:
        return f"No crontab for user {user}"

    lines = result.stdout.splitlines()
    filtered = [line for line in lines if pattern not in line]
    removed = len(lines) - len(filtered)

    if removed == 0:
        return f"No cron entries matched pattern {pattern!r}"

    new_crontab = "\n".join(filtered) + "\n"
    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to update crontab: {proc.stderr.strip()}"
        )

    return f"Removed {removed} cron entry/entries matching {pattern!r}"


# ── Self-update ─────────────────────────────────────────────────────────────

def _update_agent(params: dict, config: AgentConfig) -> str:
    """Download the latest agent binary from the server and replace this binary.

    The agent restarts itself via systemctl 3 seconds after the binary is
    replaced, giving this task result time to be reported first.
    """
    import requests as _requests

    platform = (params.get("platform") or "").strip()
    if not platform:
        if sys.platform == "win32":
            platform = "windows-amd64"
        elif sys.platform == "darwin":
            machine = os.uname().machine
            platform = "darwin-arm64" if machine == "arm64" else "darwin-amd64"
        else:
            machine = os.uname().machine
            platform = "linux-arm64" if machine in ("aarch64", "arm64") else "linux-amd64"

    url = f"{config.server_url}/agent/download/{platform}/"
    token = config.agent_token

    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()

    resp = _requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=(10, 120),
        stream=True,
    )
    resp.raise_for_status()

    tmp_fd, tmp_path = tempfile.mkstemp(dir=current_exe.parent, prefix=".vigil-agent-update-")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, current_exe)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    new_version = resp.headers.get("X-Vigil-Version", "unknown")

    def _restart_after_delay():
        time.sleep(3)
        try:
            if sys.platform == "win32":
                subprocess.run(["sc", "stop", "vigil-agent"], timeout=10, capture_output=True)
                time.sleep(2)
                subprocess.run(["sc", "start", "vigil-agent"], timeout=10, capture_output=True)
            elif sys.platform == "darwin":
                subprocess.run(
                    ["launchctl", "stop", "com.susquehannasyntax.vigil-agent"],
                    timeout=10, capture_output=True,
                )
                time.sleep(1)
                subprocess.run(
                    ["launchctl", "start", "com.susquehannasyntax.vigil-agent"],
                    timeout=10, capture_output=True,
                )
            else:
                subprocess.run(
                    ["systemctl", "restart", "vigil-agent"],
                    timeout=10, capture_output=True,
                )
        except Exception:
            pass

    t = threading.Thread(target=_restart_after_delay, daemon=True)
    t.start()

    return f"Agent updated to {new_version} ({platform}); restarting in 3 s"


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
    "request_nessus_scan": _request_nessus_scan,
    "remove_container": _remove_container,
    "docker_compose_up": _docker_compose_up,
    "docker_compose_down": _docker_compose_down,
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
    # Networking
    "add_firewall_rule": _add_firewall_rule,
    "remove_firewall_rule": _remove_firewall_rule,
    # User management
    "create_user": _create_user,
    "delete_user": _delete_user,
    "add_user_to_group": _add_user_to_group,
    # Cron
    "create_cron_job": _create_cron_job,
    "delete_cron_job": _delete_cron_job,
    # Self-management
    "update_agent": _update_agent,
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
) -> str:
    """Execute a single action after allowlist validation.

    This is the primary entry point used by both the legacy single-step path
    and the multi-step ``TaskRuntime``.  Each action is validated individually
    against the agent's local mode/allowlist — a compromised server cannot
    escalate privileges beyond what the agent config permits.

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

    return handler(params, config)


# Backward-compatible alias — the agent's __main__.py calls this for
# single-action tasks.
execute_task = execute_action
