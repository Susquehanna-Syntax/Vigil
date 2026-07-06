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


_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def _docker_inspect(ref: str, *, kind: str = "container") -> dict:
    """Return the parsed ``docker inspect`` object for a container or image."""
    out = _run(["docker", "inspect", "--type", kind, ref])
    data = json.loads(out)
    if not data:
        raise RuntimeError(f"docker inspect returned nothing for {ref!r}")
    return data[0]


def _recreate_run_args(spec: dict, old_image: dict, new_image_ref: str) -> list[str]:
    """Build the ``docker run`` argv that reproduces *spec* on a new image.

    Only configuration the *user* supplied at ``docker run`` time is carried
    over — env vars, command, and entrypoint are diffed against the original
    image's defaults so the new image's own defaults still apply (the same
    approach watchtower uses). Exotic host configs (tmpfs, GPUs, log drivers,
    resource limits) are not reproduced; those setups belong in compose.
    """
    cfg = spec.get("Config") or {}
    host = spec.get("HostConfig") or {}
    img_cfg = old_image.get("Config") or {}

    args = ["docker", "run", "-d", "--name", (spec.get("Name") or "").lstrip("/")]

    restart = host.get("RestartPolicy") or {}
    policy = restart.get("Name") or ""
    if policy and policy != "no":
        retries = restart.get("MaximumRetryCount") or 0
        if policy == "on-failure" and retries:
            policy = f"{policy}:{retries}"
        args += ["--restart", policy]

    image_env = set(img_cfg.get("Env") or [])
    for env in cfg.get("Env") or []:
        if env not in image_env:
            args += ["-e", env]

    image_labels = img_cfg.get("Labels") or {}
    for label, value in (cfg.get("Labels") or {}).items():
        if image_labels.get(label) != value:
            args += ["--label", f"{label}={value}"]

    network = host.get("NetworkMode") or "default"
    if network not in ("default", "bridge"):
        args += ["--network", network]

    if host.get("PublishAllPorts"):
        args.append("-P")
    for port, bindings in (host.get("PortBindings") or {}).items():
        for binding in bindings or [{}]:
            host_ip = binding.get("HostIp") or ""
            host_port = binding.get("HostPort") or ""
            if host_ip:
                args += ["-p", f"{host_ip}:{host_port}:{port}"]
            elif host_port:
                args += ["-p", f"{host_port}:{port}"]
            else:
                args += ["-p", port]

    for mount in spec.get("Mounts") or []:
        source = mount.get("Source") if mount.get("Type") == "bind" else mount.get("Name")
        if not source:
            continue
        volume = f"{source}:{mount.get('Destination')}"
        if not mount.get("RW", True):
            volume += ":ro"
        args += ["-v", volume]

    if host.get("Privileged"):
        args.append("--privileged")
    for cap in host.get("CapAdd") or []:
        args += ["--cap-add", cap]
    for cap in host.get("CapDrop") or []:
        args += ["--cap-drop", cap]
    for device in host.get("Devices") or []:
        on_host = device.get("PathOnHost")
        if on_host:
            args += ["--device", f"{on_host}:{device.get('PathInContainer') or on_host}"]
    for extra_host in host.get("ExtraHosts") or []:
        args += ["--add-host", extra_host]
    if cfg.get("User"):
        args += ["--user", cfg["User"]]

    trailing: list[str] = []
    entrypoint = cfg.get("Entrypoint")
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    if entrypoint and entrypoint != (img_cfg.get("Entrypoint") or None):
        # --entrypoint takes a single executable; the rest of the override,
        # plus the command, must be restated as trailing args.
        args += ["--entrypoint", entrypoint[0]]
        trailing += entrypoint[1:]
        trailing += cfg.get("Cmd") or []
    else:
        command = cfg.get("Cmd")
        if isinstance(command, str):
            command = [command]
        if command and command != (img_cfg.get("Cmd") or None):
            trailing += command

    args.append(new_image_ref)
    args += [str(part) for part in trailing]
    return args


def _recreate_container(params: dict, _config: AgentConfig) -> str:
    """Stop, remove, and re-run a container so it adopts a freshly pulled image.

    ``docker restart`` keeps a container on the image it was created from, so
    a pull + restart never applies an update. Applying one requires
    recreating the container: inspect the existing one, carry its
    user-supplied config (env overrides, ports, volumes, network, restart
    policy, capabilities) onto a new container on the target image, and roll
    the original back into place if the replacement fails to start.

    Compose-managed containers are refused — recreate those with
    ``docker_compose_up`` so compose stays authoritative over their config.
    """
    name = _validate_name(params.get("container_name", ""), "container name")
    spec = _docker_inspect(name)

    labels = (spec.get("Config") or {}).get("Labels") or {}
    if labels.get(_COMPOSE_PROJECT_LABEL):
        raise ValueError(
            f"Container {name!r} is managed by docker compose "
            f"(project {labels[_COMPOSE_PROJECT_LABEL]!r}) — "
            f"use docker_compose_up to recreate it"
        )

    image_ref = params.get("image") or (spec.get("Config") or {}).get("Image") or ""
    if not _SAFE_IMAGE.match(image_ref):
        raise ValueError(f"Invalid image name: {image_ref!r}")

    old_image_id = spec.get("Image") or ""
    # The old image is always inspectable while its container exists — docker
    # refuses to remove an image that a container still references.
    old_image = _docker_inspect(old_image_id or image_ref, kind="image")
    run_args = _recreate_run_args(spec, old_image, image_ref)

    backup = f"{name}.vigil-old"
    try:
        _run(["docker", "rm", "-f", backup])  # clear stale backup from a failed run
    except RuntimeError:
        pass

    _run(["docker", "stop", name])
    _run(["docker", "rename", name, backup])
    try:
        _run(run_args, timeout=300)
    except Exception as exc:
        try:
            _run(["docker", "rm", "-f", name])  # half-created replacement, if any
        except RuntimeError:
            pass
        try:
            _run(["docker", "rename", backup, name])
            _run(["docker", "start", name])
            rollback = "original container restored"
        except RuntimeError as rb_exc:
            rollback = f"ROLLBACK FAILED, backup container is {backup!r}: {rb_exc}"
        raise RuntimeError(f"Recreate failed ({rollback}): {exc}") from exc
    _run(["docker", "rm", backup])

    new_image_id = _run(["docker", "inspect", "--format", "{{.Image}}", name])
    changed = "image updated" if new_image_id != old_image_id else "image unchanged"
    return (
        f"Recreated {name} on {image_ref} ({changed})\n"
        f"  old image: {old_image_id[:19]}\n"
        f"  new image: {new_image_id[:19]}"
    )


def _request_nessus_scan(_params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server records a Nessus scan request.

    No real work happens on the agent — Nessus scans the host's IP from
    the central scanner. The server inspects completed task params for
    this action and creates a ``VulnScan(state=REQUESTED)`` row, which
    the next ``sync_vulns`` cycle launches against Nessus.
    """
    return "Nessus scan requested — central scanner will pick it up"


def _request_network_scan(params: dict, _config: AgentConfig) -> str:
    """Engine-agnostic version of ``_request_nessus_scan``.

    The agent has no opinion about which network scanner runs — that's
    the server's call. We just emit a marker; the task-completion
    handler decides Nessus vs. Greenbone based on
    ``params.engine`` (if set) or the host's preferred_scanners.
    """
    engine = (params.get("engine") or "auto").strip()
    return f"Network scan requested (engine={engine}) — server will dispatch"


# Trivy actions ─────────────────────────────────────────────────────────────
# Trivy is agent-local: the scan runs here and we ship the JSON back as
# task output. The server's task-completion handler routes the JSON into
# apps/vulns/scanners/trivy.py:TrivyScanner.ingest_report.

_TRIVY_SCOPE_PATTERN = re.compile(r"^(fs|rootfs|image:[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254})$")


# Subprocess wall-clock budget for a scan, and Trivy's own internal scan
# deadline kept just under it. Trivy's *default* --timeout is 5m, which
# routinely expires while walking a real root filesystem and surfaces as
# "semaphore acquire: context deadline exceeded" — so we set it explicitly.
_TRIVY_SUBPROCESS_TIMEOUT = 1200
_TRIVY_SCAN_TIMEOUT = _TRIVY_SUBPROCESS_TIMEOUT - 60

# Directories full of large content-addressed blobs (flatpak/docker/containers/
# snap) that aren't OS or language package sources. Walking them adds minutes
# and yields no findings — and analysing those blobs is what stalls the scan.
_TRIVY_SKIP_DIRS = (
    "/var/lib/flatpak",
    "/var/lib/docker",
    "/var/lib/containers",
    "/var/lib/snapd",
    "/var/snap",
)


def _run_trivy_scan(params: dict, _config: AgentConfig) -> str:
    """Run ``trivy`` against the local filesystem or a named image.

    ``scope`` selects what to scan:
      * ``fs`` (default) — ``trivy fs /``
      * ``rootfs`` — alias for ``fs``
      * ``image:<name>`` — ``trivy image <name>``

    Returns Trivy's raw JSON output. The server parses it on receipt —
    we don't attempt any local interpretation here.

    The scan is restricted to the ``vuln`` scanner: a default ``fs`` scan also
    runs the *secret* scanner, which reads and analyses every file on disk.
    That's both wasted work (the server only ingests vulnerabilities) and the
    usual cause of stalls on hosts with large blob stores. Combined with an
    explicit ``--timeout`` and a skip-list, scans complete reliably.
    """
    if shutil.which("trivy") is None:
        raise RuntimeError(
            "trivy binary not found in PATH — install it with "
            "'curl -sSL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh' "
            "or run the 'Install Trivy' task template"
        )

    scope = (params.get("scope") or "fs").strip()
    if not _TRIVY_SCOPE_PATTERN.match(scope):
        raise ValueError(f"Invalid trivy scope: {scope!r}")

    common = [
        "--quiet",
        "--format", "json",
        "--severity", "CRITICAL,HIGH,MEDIUM,LOW",
        "--scanners", "vuln",
        "--timeout", f"{_TRIVY_SCAN_TIMEOUT}s",
    ]
    if scope in ("fs", "rootfs"):
        skip = []
        for d in _TRIVY_SKIP_DIRS:
            # Under `trivy fs /` the walker matches paths *relative* to the
            # scan root (e.g. "var/lib/flatpak/…", no leading slash), so an
            # absolute --skip-dirs may not match. Pass both forms to be safe.
            skip += ["--skip-dirs", d, "--skip-dirs", d.lstrip("/")]
        cmd = ["trivy", "fs", *common, *skip, "/"]
    else:
        # scope = "image:<name>"
        image_name = scope[len("image:"):]
        if not _SAFE_IMAGE.match(image_name):
            raise ValueError(f"Invalid image name in trivy scope: {image_name!r}")
        cmd = ["trivy", "image", *common, image_name]

    return _run(cmd, timeout=_TRIVY_SUBPROCESS_TIMEOUT)


def _trivy_db_update(_params: dict, _config: AgentConfig) -> str:
    """Force a refresh of Trivy's local vulnerability database.

    Trivy auto-updates on first scan but caches between runs; this
    action is for explicit refreshes (e.g. after a security advisory).
    """
    if shutil.which("trivy") is None:
        raise RuntimeError("trivy binary not found in PATH")
    return _run(["trivy", "--quiet", "image", "--download-db-only"], timeout=300)


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
    return f"Copied {src} -> {dest}"


def _move_file(params: dict, config: AgentConfig) -> str:
    # A move both writes the destination and removes the source, so both
    # ends go through the sensitive-path checks.
    src = _validate_write_path(params.get("src", ""), config)
    dest = _validate_write_path(params.get("dest", ""), config)
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
        subprocess.run(["systemctl", "daemon-reload"], timeout=10, capture_output=True)
        return "applied proxy drop-in from /etc/environment"
    except Exception as exc:
        logger.warning("Proxy drop-in sync failed: %s", exc)
        return f"failed: {exc}"


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
    "recreate_container": _recreate_container,
    "request_nessus_scan": _request_nessus_scan,
    "request_network_scan": _request_network_scan,
    "run_trivy_scan": _run_trivy_scan,
    "trivy_db_update": _trivy_db_update,
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
