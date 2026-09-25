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

import hashlib
import json
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

from . import collector  # noqa: F401 — tests patch executor.collector
from . import firewall
from . import scripthash
from .config import AgentConfig
from .deferral import RebootDeferral
from .pkg_manager import detect as detect_pkg_manager
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


# ── Service management ──────────────────────────────────────────────────────


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


def _container_running(name: str) -> bool:
    try:
        return _run(["docker", "inspect", "--format", "{{.State.Running}}", name]).strip() == "true"
    except RuntimeError:
        return False


def _restart_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "restart", name]), {"active": _service_is_active(name)})


def _start_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "start", name]), {"active": _service_is_active(name)})


def _stop_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "stop", name]), {"active": _service_is_active(name)})


def _reload_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "reload", name]), {"active": _service_is_active(name)})


def _enable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "enable", name]), {"enabled": _service_is_enabled(name)})


def _disable_service(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("service_name", ""), "service name")
    return ActionOutput(_run(["systemctl", "disable", name]), {"enabled": _service_is_enabled(name)})


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


# ── Container, tag and scan handlers ──────────────────────────────────────
# Live in actions/containers.py and actions/tags_scans.py; re-exported below.


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

    return ActionOutput(f"Wrote {len(content)} bytes to {path}",
                        {"path": str(path), "bytes": len(content)})


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

    return ActionOutput(f"Created directory {path}", {"path": str(path)})


def _delete_path(params: dict, _config: AgentConfig) -> str:
    recursive = bool(params.get("recursive", False))
    path = _validate_delete_path(params.get("path", ""), recursive)

    if path.is_dir():
        shutil.rmtree(path)
        return ActionOutput(f"Deleted directory {path} (recursive)",
                            {"path": str(path), "recursive": recursive})
    else:
        path.unlink()
        return ActionOutput(f"Deleted {path}",
                            {"path": str(path), "recursive": recursive})


def _copy_file(params: dict, config: AgentConfig) -> str:
    src = _validate_path(params.get("src", ""), "src")
    # The destination is written to, so it gets the sensitive-file checks —
    # _validate_path alone would let a copy land on /etc/shadow.
    dest = _validate_write_path(params.get("dest", ""), config)
    if not src.exists():
        raise ValueError(f"Source not found: {src}")

    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return ActionOutput(f"Copied {src} -> {dest}", {"src": str(src), "dest": str(dest)})


def _move_file(params: dict, config: AgentConfig) -> str:
    # A move both writes the destination and removes the source, so both
    # ends go through the sensitive-path checks.
    src = _validate_write_path(params.get("src", ""), config)
    dest = _validate_write_path(params.get("dest", ""), config)
    if not src.exists():
        raise ValueError(f"Source not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return ActionOutput(f"Moved {src} -> {dest}", {"src": str(src), "dest": str(dest)})


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
    return ActionOutput(f"Set {', '.join(parts)} on {path}", {"path": str(path)})


# ── Package management ──────────────────────────────────────────────────────


def _assert_initramfs_clean(output: str) -> str:
    from .pkg_manager import poisoned_initramfs

    poisoned = poisoned_initramfs()
    if not poisoned:
        return output
    raise RuntimeError(
        "Package operation completed but these initramfs images contain "
        "ephemeral PyInstaller paths and will NOT boot: "
        f"{', '.join(poisoned)}. Rebuild each from an interactive root shell "
        "with `update-initramfs -u -k <version>` before rebooting this host. "
        f"Package output follows:\n{output}"
    )


def _install_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    text = _assert_initramfs_clean(pm.install(pkg_name))
    return ActionOutput(text, {"package": pkg_name, "manager": pm.name,
                               "installed_version": pm.installed_version(pkg_name)})


def _remove_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    return ActionOutput(pm.remove(pkg_name), {"package": pkg_name, "manager": pm.name})


def _update_package(params: dict, _config: AgentConfig) -> str:
    pkg_name = params.get("package_name", "")
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")
    pm.refresh()
    text = _assert_initramfs_clean(pm.install(pkg_name))  # install upgrades if already present
    return ActionOutput(text, {"package": pkg_name, "manager": pm.name,
                               "installed_version": pm.installed_version(pkg_name)})


def _run_package_updates(params: dict, _config: AgentConfig) -> str:
    security_only = params.get("security_only", False)
    pm = detect_pkg_manager()
    if pm is None:
        raise RuntimeError("No supported package manager found")

    pm.refresh()
    outputs = {"manager": pm.name, "security_only": bool(security_only)}

    if security_only:
        # Security-only upgrades only supported for apt and dnf
        if pm.name in ("apt", "apt-get"):
            return ActionOutput(_assert_initramfs_clean(_run(
                ["apt-get", "upgrade", "-y", "-qq",
                 "-o", "Dir::Etc::SourceList=/etc/apt/sources.list"],
                timeout=600,
            )), outputs)
        if pm.name == "dnf":
            return ActionOutput(_assert_initramfs_clean(_run(
                ["dnf", "update", "-y", "-q", "--security"], timeout=600
            )), outputs)
        logger.warning(
            "security_only not supported for %s, running full upgrade",
            pm.name,
        )

    return ActionOutput(_assert_initramfs_clean(pm.upgrade_all()), outputs)


# ── System ──────────────────────────────────────────────────────────────────


def _clear_temp_files(params: dict, _config: AgentConfig) -> str:
    """Delete files in the system temp directory older than N days.

    Done in Python rather than by shelling out. The previous implementation ran

        find /tmp -type f -mtime +N -delete

    unconditionally, which on Windows resolved "find" to
    C:\\Windows\\System32\\FIND.exe — a string-search tool that shares only a
    name — and failed with "FIND: Invalid switch". /tmp does not exist there
    either. Doing the walk here means one implementation, no shell, and the
    same semantics everywhere.
    """
    days = int(params.get("older_than_days", 7))
    if days < 0:
        raise ValueError("older_than_days must be non-negative")

    temp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    skipped = 0
    for path in temp_root.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            st = path.stat()
            if st.st_mtime >= cutoff:
                continue
            size = st.st_size
            path.unlink()
        except OSError:
            # A temp directory always has files something else holds open,
            # and on Windows that is the normal case rather than the
            # exception. One locked file must not fail the whole task.
            skipped += 1
            continue
        removed += 1
        freed += size

    return ActionOutput(
        (f"Removed {removed} file(s) older than {days} day(s) from "
         f"{temp_root}, freeing {freed // 1024} KiB"
         + (f"; {skipped} in use or not permitted" if skipped else "")),
        {"removed": removed, "skipped": skipped})


def _input_env(inputs: dict | None) -> dict[str, str]:
    """Map task inputs to ``VIGIL_INPUT_<NAME>`` environment variables.

    Inputs pasted into a command line would be code — ``nginx; rm -rf /``
    runs as a second command.  In an environment variable the same value is
    only data: ``printf '%s' "$VIGIL_INPUT_APP"`` prints it.
    """
    if not inputs:
        return {}
    env: dict[str, str] = {}
    for name, value in inputs.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            continue
        if isinstance(value, str) and "\x00" in value:
            raise ValueError(f"input {name!r} contains a NUL byte")
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, float) and value.is_integer():
            value = str(int(value))
        else:
            value = str(value)
        env[f"VIGIL_INPUT_{str(name).upper()}"] = value
    return env


def _execute_inline_script(params: dict, config: AgentConfig, inputs: dict | None = None) -> str:
    """Run an inline ``script`` body with ``shell``.

    The body is arbitrary code from the server, so outside full_control it runs
    only if its exact hash is in ``allowed_script_hashes`` — approved on this
    host by its owner. Any edit changes the hash and needs a new approval.
    """
    if "script_name" in params:
        raise ValueError("give script_name or script, not both")

    shell = params.get("shell", "")
    if shell not in ("bash", "sh", "powershell", "pwsh"):
        raise ValueError(
            f"shell must be one of bash, sh, powershell, pwsh; got {shell!r}")

    body = params.get("script", "")
    if not isinstance(body, str) or not body:
        raise ValueError("script must be a non-empty string")
    if len(body) > 65536:
        raise ValueError("script body exceeds 65536 characters")

    digest = scripthash.script_hash(body)
    if config.mode != "full_control" and digest not in config.allowed_script_hashes:
        raise ValueError(
            f"script hash not allowlisted: {digest} — approve it on this host with "
            f"`vigil-agent allow-script` or add it to allowed_script_hashes in agent.yml")

    timeout = int(params.get("timeout", _EXEC_TIMEOUT))
    if timeout < 1 or timeout > 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")

    is_powershell = shell in ("powershell", "pwsh")
    tmp_dir = Path(tempfile.mkdtemp(prefix="vigil-script-"))
    try:
        script_path = tmp_dir / ("script.ps1" if is_powershell else "script.sh")
        script_path.write_text(scripthash.normalise(body), encoding="utf-8")
        if os.name == "posix":
            script_path.chmod(0o700)
        if is_powershell:
            cmd = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                   "Bypass", "-File", str(script_path)]
        else:
            cmd = [shell, str(script_path)]
        output = _run(cmd, timeout=timeout, extra_env=_input_env(inputs))
        return ActionOutput(f"[{digest}]\n{output}", {"exit_code": 0})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _execute_script(params: dict, config: AgentConfig, inputs: dict | None = None) -> str:
    if "script" in params:
        return _execute_inline_script(params, config, inputs)

    script_name = params.get("script_name", "")
    if not _SAFE_SCRIPT_NAME.match(script_name):
        raise ValueError(f"Invalid script name: {script_name!r}")

    scripts_dir = config.scripts_dir.resolve()
    script_path = (scripts_dir / script_name).resolve()

    # Path traversal protection. is_relative_to() rather than a string
    # startswith on str(scripts_dir) + "/": that hardcoded separator never
    # matches a Windows path, so every script was refused there — fail-safe,
    # but it meant execute_script could not work on Windows at all.
    if not script_path.is_relative_to(scripts_dir):
        raise ValueError(
            f"Script path escapes scripts directory: {script_name!r}"
        )

    if not script_path.is_file():
        raise ValueError(f"Script not found: {script_name}")

    if os.name == "posix":
        # POSIX mode bits only. Python synthesises st_mode on Windows, so this
        # check there is meaningless and its remedy — chmod — is not a command
        # the operator has. Windows access is governed by the ACL on
        # scripts_dir, which the installer restricts.
        st = script_path.stat()
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(
                f"Script {script_name} is writable by group/others — refusing "
                f"to execute. Run: chmod go-w {script_path}"
            )

    return ActionOutput(
        _run([str(script_path)], extra_env=_input_env(inputs)),
        {"exit_code": 0},
    )


def _sanitize_notify_message(raw: str) -> str:
    """Cap at 200 chars and strip anything outside the conservative
    allowlist, before the text reaches a command line."""
    text = re.sub(r"[^A-Za-z0-9 .,:;!?()\-'']", "", raw)[:200]
    return " ".join(text.split())


def _notify_user(message: str) -> None:
    """Show the user a desktop notification. Any failure is logged and
    swallowed — a missing notify tool must never become a reboot-blocker."""
    if sys.platform == "win32":
        cmd = ["msg", "*", message]
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e",
               'display notification "' + message + '" with title "Vigil"']
    else:
        cmd = ["notify-send", "Vigil", message]
    try:
        _run(cmd, timeout=15)
    except FileNotFoundError:
        if sys.platform != "darwin":
            try:
                _run(["wall", message], timeout=15)
            except Exception:
                logger.warning("Reboot notification failed: wall unavailable")
        else:
            logger.warning("Reboot notification failed: %s", "notify-send")
    except Exception:
        logger.warning("Reboot notification failed", exc_info=True)


def _reboot(params: dict, config: AgentConfig) -> str:
    delay = int(params.get("delay_seconds", 0))
    if delay < 0:
        raise ValueError("delay_seconds must be non-negative")
    defer_limit = int(params.get("defer_limit", 0))
    if not 0 <= defer_limit <= 8:
        raise ValueError("defer_limit must be between 0 and 8")
    defer_minutes = int(params.get("defer_minutes", 0))
    if defer_minutes and not 5 <= defer_minutes <= 240:
        raise ValueError("defer_minutes must be between 5 and 240")
    message = _sanitize_notify_message(str(params.get("notify_message", "")))

    task_id = str(params.get("task_id", ""))
    deferral = RebootDeferral(config.data_dir)
    deferral_active = bool(
        task_id and (defer_limit > 0 or defer_minutes > 0)
        and deferral.expiry_remaining() > 0
        and deferral.exhausted() is False
    )
    # A new task id resets the deferral budget; the same id keeps counting.
    if task_id:
        deferral.record(task_id, defer_limit, defer_minutes)
    # Capture exhaustion before clear(): a user who has used up every
    # deferral may no longer block the reboot, so it must be forced.
    force_close = defer_limit <= 0 or deferral.exhausted()
    if deferral.expired():
        deferral.clear()
        deferral_active = False

    if sys.platform == "win32":
        # Windows shutdown.exe: /t takes raw seconds.
        argv = ["shutdown", "/r", "/t", str(delay)]
        # -r +N on Linux takes MINUTES while /t takes SECONDS — do not
        # "tidy" the two into the same unit.
        if message:
            argv += ["/c", message]
        # /f forces apps closed. Only when no deferral is in play: a
        # fresh dispatch with a budget, or a dispatch inside an active
        # deferral window, must not rip the user's work away; but once
        # the budget is exhausted the user may no longer block it.
        if force_close:
            argv.append("/f")
    else:
        # coreutils shutdown: -r now / +N (minutes).
        if delay == 0:
            argv = ["shutdown", "-r", "now"]
        else:
            minutes = max(1, delay // 60)
            argv = ["shutdown", "-r", f"+{minutes}"]

    if params.get("notify") and message:
        _notify_user(message)
    output = _run(argv)
    deferral.clear()
    return ActionOutput(
        output, {"delay_seconds": delay, "deferral_active": bool(deferral_active)})


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

    return ActionOutput(_run(shlex.split(command), timeout=timeout),
                        {"exit_code": 0})


def _set_hostname(params: dict, _config: AgentConfig) -> str:
    hostname = params.get("hostname", "")
    if not _SAFE_HOSTNAME.match(hostname):
        raise ValueError(f"Invalid hostname: {hostname!r}")
    return ActionOutput(
        _run(["hostnamectl", "set-hostname", hostname]), {"hostname": hostname})


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
    source = firewall.validate_source(params.get("source", "any"))
    interface = firewall.validate_interface(params.get("interface", ""))

    backend = firewall.detect()
    if backend is None:
        raise RuntimeError(
            "No supported firewall tool found (ufw, firewall-cmd, or Windows)")
    return ActionOutput(
        backend.add_rule(port, protocol, action, source, interface),
        {"port": str(port), "protocol": str(protocol), "action": str(action)})


def _remove_firewall_rule(params: dict, _config: AgentConfig) -> str:
    port = int(params.get("port", 0))
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    protocol = str(params.get("protocol", "tcp")).lower()
    if protocol not in ("tcp", "udp"):
        raise ValueError(f"Protocol must be tcp or udp, got {protocol!r}")
    action = str(params.get("action", "allow")).lower()
    if action not in ("allow", "deny"):
        raise ValueError(f"Action must be allow or deny, got {action!r}")
    source = firewall.validate_source(params.get("source", "any"))
    # Optional: only WindowsBackend uses these, and it validates rule_id
    # itself (see firewall.validate_rule_name) before it ever reaches
    # PowerShell -- ufw and firewall-cmd ignore both. `name` (DisplayName)
    # is carried for display/back-compat only; WindowsBackend removes by
    # `rule_id` (the unique Name/InstanceID), never by `name` -- DisplayName
    # is not guaranteed unique. See firewall.WindowsBackend.remove_rule.
    name = str(params.get("name", "") or "")
    rule_id = str(params.get("rule_id", "") or "")

    backend = firewall.detect()
    if backend is None:
        raise RuntimeError(
            "No supported firewall tool found (ufw, firewall-cmd, or Windows)")
    return ActionOutput(
        backend.remove_rule(port, protocol, action, source,
                            name=name, rule_id=rule_id),
        {"port": str(port), "protocol": str(protocol), "action": str(action)})


def _set_firewall_policy(params: dict, _config: AgentConfig) -> str:
    direction = str(params.get("direction", "")).lower()
    if direction not in ("incoming", "outgoing"):
        raise ValueError(
            f"Direction must be incoming or outgoing, got {direction!r}")
    policy = str(params.get("policy", "")).lower()
    if policy not in ("allow", "deny", "reject"):
        raise ValueError(
            f"Policy must be allow, deny or reject, got {policy!r}")
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(
        backend.set_policy(direction, policy),
        {"direction": direction, "policy": policy})


def _enable_firewall(_params: dict, _config: AgentConfig) -> str:
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(backend.set_enabled(True), {"enabled": True})


def _disable_firewall(_params: dict, _config: AgentConfig) -> str:
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(backend.set_enabled(False), {"enabled": False})


def _list_firewall_rules(_params: dict, _config: AgentConfig) -> str:
    """Return this host's firewall state as JSON.

    A host with no supported firewall tool is a normal answer, not an error:
    raising here would mark the task failed and bury the one fact the operator
    needs behind a stack trace.
    """
    backend = firewall.detect()
    if backend is None:
        snapshot = {
            "tool": None, "supported": False, "enabled": False,
            "defaults": {"incoming": "unknown", "outgoing": "unknown"},
            "rules": [], "unparsed": [],
        }
        return ActionOutput(
            json.dumps(snapshot),
            {"supported": False, "enabled": False, "rule_count": 0})
    snapshot = backend.snapshot()
    snapshot["supported"] = True
    return ActionOutput(
        json.dumps(snapshot),
        {"supported": bool(snapshot.get("supported", True)),
         "enabled": bool(snapshot.get("enabled", False)),
         "rule_count": len(snapshot.get("rules", []))})


# ── Windows Update ────────────────────────────────────────────────────────


def _windows_update_scan(params: dict, _config: AgentConfig) -> str:
    """Return pending Windows updates as JSON, filtered by the optional
    ``classifications`` / ``include_kb`` / ``exclude_kb`` / ``severity_floor``
    params (the same filters the install action applies)."""
    from . import windows_update

    backend = windows_update.detect()
    if backend is None:
        raise ValueError("windows_update_scan is only supported on Windows")
    updates = windows_update.filter_updates(
        backend.scan(),
        classifications=params.get("classifications"),
        include_kb=params.get("include_kb"),
        exclude_kb=params.get("exclude_kb"),
        severity_floor=params.get("severity_floor"),
    )
    return ActionOutput(json.dumps({
        "supported": True,
        "count": len(updates),
        "updates": updates,
    }), {"count": len(updates)})


def _windows_update_install(params: dict, _config: AgentConfig) -> str:
    """Install the pending Windows updates that survive the filter params.

    Never reboots: the result reports ``reboot_required`` and the agent stops
    there. ``reboot`` is a separate action; an install action that reboots a
    machine is the failure this milestone exists to prevent.
    """
    from . import windows_update

    backend = windows_update.detect()
    if backend is None:
        raise ValueError("windows_update_install is only supported on Windows")
    updates = windows_update.filter_updates(
        backend.scan(),
        classifications=params.get("classifications"),
        include_kb=params.get("include_kb"),
        exclude_kb=params.get("exclude_kb"),
        severity_floor=params.get("severity_floor"),
    )
    # Install() on an empty collection throws a COM error that reads like a
    # real failure, so an empty filter result is reported, not installed.
    if not updates:
        return ActionOutput(json.dumps({
            "supported": True,
            "result_code": windows_update.RESULT_NOT_STARTED,
            "reboot_required": False,
            "installed": [],
            "failed": [],
            "detail": "no updates matched the filter; nothing installed",
        }), {"installed_count": 0, "failed_count": 0, "reboot_required": False})
    result = backend.install([u["update_id"] for u in updates])
    result["supported"] = True
    return ActionOutput(
        json.dumps(result),
        {"installed_count": len(result["installed"]),
         "failed_count": len(result["failed"]),
         "reboot_required": bool(result["reboot_required"])})


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
    return ActionOutput(_run(cmd), {"username": username})


def _delete_user(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    cmd = ["userdel"]
    if params.get("remove_home", False):
        cmd.append("--remove")
    cmd.append(username)
    return ActionOutput(_run(cmd), {"username": username})


def _add_user_to_group(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    group = params.get("group", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    if not _SAFE_GROUP.match(group):
        raise ValueError(f"Invalid group name: {group!r}")
    return ActionOutput(
        _run(["usermod", "-aG", group, username]),
        {"username": username, "group": group})


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
            env=clean_env(),
        )
        current = existing.stdout if existing.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        current = ""

    new_crontab = current.rstrip("\n") + "\n" + cron_line + "\n"

    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
        env=clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set crontab: {proc.stderr.strip()}")

    return ActionOutput(
        f"Added cron job for user {user}: {cron_line}", {"user": user})


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
        env=clean_env(),
    )
    if result.returncode != 0:
        return ActionOutput(f"No crontab for user {user}",
                            {"user": user, "removed": 0})

    lines = result.stdout.splitlines()
    filtered = [line for line in lines if pattern not in line]
    removed = len(lines) - len(filtered)

    if removed == 0:
        return ActionOutput(f"No cron entries matched pattern {pattern!r}",
                            {"user": user, "removed": 0})

    new_crontab = "\n".join(filtered) + "\n"
    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
        env=clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to update crontab: {proc.stderr.strip()}"
        )

    return ActionOutput(
        f"Removed {removed} cron entry/entries matching {pattern!r}",
        {"user": user, "removed": removed})


# ── Self-update ─────────────────────────────────────────────────────────────

def _sha256_file(path) -> str:
    """Return the lowercase hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_systemd_proxy(config: AgentConfig) -> str:
    """Mirror the host's ``/etc/environment`` proxy into the agent's unit.

    systemd services don't inherit a login shell's environment, so on a
    proxied network the agent — and any task that shells out to
    curl/wget, like installing Trivy — can't reach the internet even
    though the host can. On every self-update we read ``/etc/environment``
    (the standard system-wide env file) and, if it defines an HTTP(S)
    proxy, drop it into a ``vigil-agent.service.d`` override so the
    post-update restart comes up with working egress. Loopback and the
    Vigil server stay direct.

    Best-effort and Linux/systemd only — any failure is logged and
    ignored so it can never break the binary swap that already happened.
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return "skipped (not linux/systemd)"
    env_file = Path("/etc/environment")
    if not env_file.exists():
        return "no /etc/environment"
    try:
        vals: dict[str, str] = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.lower().startswith("export "):
                key = key[len("export "):].strip()
            key = key.lower()
            val = val.strip().strip('"').strip("'")
            if key in ("http_proxy", "https_proxy", "no_proxy") and val:
                vals[key] = val

        http_p = vals.get("http_proxy", "")
        https_p = vals.get("https_proxy", "")
        if not http_p and not https_p:
            return "no proxy in /etc/environment"

        # Keep loopback + the Vigil server direct so internal check-ins
        # never detour through the proxy.
        server_host = re.sub(r"^https?://", "", config.server_url or "").split("/")[0].split(":")[0]
        no_proxy_parts = ["localhost", "127.0.0.1"]
        if server_host:
            no_proxy_parts.append(server_host)
        if vals.get("no_proxy"):
            no_proxy_parts.append(vals["no_proxy"])
        no_proxy = ",".join(no_proxy_parts)

        lines = ["[Service]"]
        for name, value in (("HTTP_PROXY", http_p), ("HTTPS_PROXY", https_p)):
            if value:
                lines.append(f"Environment={name}={value}")
                lines.append(f"Environment={name.lower()}={value}")
        lines.append(f"Environment=NO_PROXY={no_proxy}")
        lines.append(f"Environment=no_proxy={no_proxy}")
        content = "\n".join(lines) + "\n"

        drop_dir = Path("/etc/systemd/system/vigil-agent.service.d")
        drop_dir.mkdir(parents=True, exist_ok=True)
        (drop_dir / "10-vigil-proxy.conf").write_text(content)
        subprocess.run(["systemctl", "daemon-reload"], timeout=10,
                       capture_output=True, env=clean_env())
        return "applied proxy drop-in from /etc/environment"
    except Exception as exc:
        logger.warning("Proxy drop-in sync failed: %s", exc)
        return f"failed: {exc}"


def _reprovision_preflight(params: dict, config: AgentConfig) -> str:
    from . import reprovision

    return reprovision.preflight(params, config)


def _reprovision_stage(params: dict, config: AgentConfig) -> str:
    from . import reprovision

    return reprovision.stage(params, config)


def _reprovision_commit(params: dict, config: AgentConfig) -> str:
    from . import reprovision

    return reprovision.commit(params, config)


def _reprovision_cleanup(params: dict, config: AgentConfig) -> str:
    from . import reprovision

    return reprovision.cleanup(params, config)


def _looks_like_zip(path: str) -> bool:
    """True when the downloaded artifact is a zip rather than a bare binary."""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def _stage_onedir_update(archive_path: str, current_exe: Path) -> None:
    """Unpack a onedir update beside the install and swap it in on restart.

    Windows cannot replace a running executable — the file is locked for as
    long as the process lives, and a onedir build is a whole directory of DLLs
    besides. So the new build is extracted next to the old one and a detached
    helper does the swap once the service has actually stopped.

    Refuses rather than improvises if the layout is not what it expects. A
    half-swapped agent directory is worse than a failed update: the update can
    be retried, a broken install needs someone at the machine.
    """
    import zipfile

    install_dir = current_exe.parent
    if sys.platform != "win32":
        raise ValueError(
            "received a zip agent artifact on a non-Windows platform; "
            "refusing to self-update")

    staging = install_dir.parent / (install_dir.name + ".new")
    backup = install_dir.parent / (install_dir.name + ".old")
    for path in (staging, backup):
        shutil.rmtree(path, ignore_errors=True)

    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(staging)
    os.unlink(archive_path)

    if not list(staging.rglob(current_exe.name)):
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(
            f"the update archive contains no {current_exe.name}; "
            "refusing to swap it in")

    # A detached cmd: this process is about to be stopped, so the swap cannot
    # run inside it. Waits for the service to stop before touching anything.
    script = install_dir.parent / "vigil-agent-update.cmd"
    script.write_text(
        "@echo off\r\n"
        "sc stop vigil-agent >nul 2>&1\r\n"
        "for /l %%i in (1,1,30) do (\r\n"
        '  sc query vigil-agent | find "STOPPED" >nul && goto swap\r\n'
        "  timeout /t 1 /nobreak >nul\r\n"
        ")\r\n"
        ":swap\r\n"
        f'rmdir /s /q "{backup}" >nul 2>&1\r\n'
        f'move "{install_dir}" "{backup}" >nul 2>&1\r\n'
        f'move "{staging}" "{install_dir}" >nul 2>&1\r\n'
        f'if not exist "{install_dir}\\{current_exe.name}" '
        f'move "{backup}" "{install_dir}" >nul 2>&1\r\n'
        "sc start vigil-agent >nul 2>&1\r\n"
        f'rmdir /s /q "{backup}" >nul 2>&1\r\n'
        # Delete the helper itself. "start /b cmd /c del" detaches so the
        # script is not holding its own file open when the delete lands;
        # without it every update leaves a .cmd in Program Files.
        f'start /b "" cmd /c del /q "{script}"\r\n',
        encoding="ascii")
    subprocess.Popen(
        ["cmd", "/c", "start", "/b", "", str(script)],
        env=clean_env(), close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


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

    # The replacement binary is verified against a SHA-256 the server placed
    # inside this Ed25519-signed task. A TLS-only download is not a strong
    # enough proof for swapping the whole agent executable, so without a
    # verified digest we refuse to self-update.
    sha_map = params.get("binary_sha256")
    expected_sha = ""
    if isinstance(sha_map, dict):
        expected_sha = str(sha_map.get(platform) or "").strip().lower()
    if not expected_sha:
        raise ValueError(
            f"update_agent task carries no verified SHA-256 for platform "
            f"{platform!r}; refusing to self-update"
        )

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
        actual_sha = _sha256_file(tmp_path)
        if actual_sha != expected_sha:
            raise ValueError(
                f"Downloaded agent binary failed SHA-256 verification: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        if _looks_like_zip(tmp_path):
            # Windows ships a PyInstaller --onedir build as a zip, because a
            # --onefile executable cannot host a Windows service. Writing that
            # archive over the service binary would replace the agent with a
            # zip file and brick the host, so the whole directory is swapped
            # instead, after the service has stopped and released its files.
            _stage_onedir_update(tmp_path, current_exe)
        else:
            os.chmod(tmp_path, 0o755)
            os.replace(tmp_path, current_exe)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    new_version = resp.headers.get("X-Vigil-Version", "unknown")

    # Self-heal proxy egress: reload the unit with any /etc/environment
    # proxy *before* the restart below picks up the new binary, so a
    # proxied host comes back online able to reach the internet.
    proxy_status = _sync_systemd_proxy(config)
    logger.info("update_agent proxy sync: %s", proxy_status)

    def _restart_after_delay():
        time.sleep(3)
        try:
            if sys.platform == "win32":
                subprocess.run(["sc", "stop", "vigil-agent"], timeout=10,
                               capture_output=True, env=clean_env())
                time.sleep(2)
                subprocess.run(["sc", "start", "vigil-agent"], timeout=10,
                               capture_output=True, env=clean_env())
            elif sys.platform == "darwin":
                subprocess.run(
                    ["launchctl", "stop", "com.susquehannasyntax.vigil-agent"],
                    timeout=10, capture_output=True, env=clean_env(),
                )
                time.sleep(1)
                subprocess.run(
                    ["launchctl", "start", "com.susquehannasyntax.vigil-agent"],
                    timeout=10, capture_output=True, env=clean_env(),
                )
            else:
                subprocess.run(
                    ["systemctl", "restart", "vigil-agent"],
                    timeout=10, capture_output=True, env=clean_env(),
                )
        except Exception:
            pass

    t = threading.Thread(target=_restart_after_delay, daemon=True)
    t.start()

    return ActionOutput(f"Agent updated to {new_version} ({platform}); restarting in 3 s",
                        {"version": new_version})


# Handlers split into modules (re-exported so executor.<name> keeps working).
from .actions.containers import (  # noqa: E402,F401
    _restart_container,
    _stop_container,
    _start_container,
    _pull_image,
    _check_docker_updates,
    _COMPOSE_PROJECT_LABEL,
    _docker_inspect,
    _recreate_run_args,
    _recreate_container,
    _update_container,
    _remove_container,
    _docker_compose_up,
    _docker_compose_down,
    _clear_docker_logs,
)
from .actions.tags_scans import (  # noqa: E402,F401
    _tag_names,
    _add_tag,
    _remove_tag,
    _request_nessus_scan,
    _request_network_scan,
    _TRIVY_SCOPE_PATTERN,
    _TRIVY_SUBPROCESS_TIMEOUT,
    _TRIVY_SCAN_TIMEOUT,
    _TRIVY_SKIP_DIRS,
    _TRIVY_VULN_FIELDS,
    _TRIVY_RESULT_BULK,
    _condense_trivy_report,
    TRIVY_GZIP_MARKER,
    _pack_report,
    _slim_report,
    _run_trivy_scan,
    _count_trivy_vulnerabilities,
    _trivy_db_update,
)
from .actions.hunts import _hunt_file


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
    "request_nessus_scan": _request_nessus_scan,
    "request_network_scan": _request_network_scan,
    "run_trivy_scan": _run_trivy_scan,
    "trivy_db_update": _trivy_db_update,
    "hunt_file": _hunt_file,
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
    "list_firewall_rules": _list_firewall_rules,
    "set_firewall_policy": _set_firewall_policy,
    "enable_firewall": _enable_firewall,
    "disable_firewall": _disable_firewall,
    # Windows Update
    "windows_update_scan": _windows_update_scan,
    "windows_update_install": _windows_update_install,
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
