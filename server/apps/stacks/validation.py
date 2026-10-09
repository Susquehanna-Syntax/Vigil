"""What a stack file and its .env may contain (M11).

A compose file deployed by Vigil runs as root's engine on the host, so it is
checked on the server before it is ever stored: it must parse, have
``services``, and not reach out of its own sandbox — no ``..`` in a bind
source, no bind of the engine socket or of the host's system directories, no
``privileged``, no host PID namespace, no ``cap_add: ALL``. Everything else
compose accepts is the operator's call.
"""

from __future__ import annotations

import re

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_COMPOSE = 200_000
MAX_ENV = 64_000

#: Host paths a stack may not bind — the engine's socket and the system.
_FORBIDDEN_BINDS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman", "/var/run/podman",
                    "/etc", "/root", "/boot", "/proc", "/sys", "/dev", "/usr", "/bin", "/sbin",
                    "/lib", "/var/lib/docker", "/var/lib/containers", "/opt/vigil")


class StackError(ValueError):
    #: 1-based line of a YAML error, when the parser reported one.
    line = None


#: Capabilities that hand a container the host (kernel modules, mounts, other
#: processes' memory, raw devices, LSM policy). NET_ADMIN and friends stay
#: allowed: VPN and network containers need them and they do not escape.
_FORBIDDEN_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "SYS_BOOT",
                   "DAC_READ_SEARCH", "BPF", "PERFMON", "MAC_ADMIN", "MAC_OVERRIDE"}
#: Host devices a stack may pass through: GPUs, a TUN device for VPNs, FUSE.
#: Namespaces a stack may not share with the host. network_mode: host stays
#: allowed; it shares ports, not the filesystem or other processes.
_HOST_NAMESPACE_KEYS = ("pid", "ipc", "userns_mode", "cgroup", "uts")


def _local_path_problem(value, what: str, name: str) -> None:
    """A path compose reads on the host must stay inside the stack's folder."""
    text = str(value or "").strip()
    if not text:
        return
    parts = text.replace("\\", "/").split("/")
    if text.startswith(("/", "~")) or ".." in parts:
        raise StackError(f"{name}: {what} must be a path inside the stack's folder, not {text}")


def _bind_source(volume) -> str:
    if isinstance(volume, dict):
        return str(volume.get("source") or "") if volume.get("type") == "bind" else ""
    text = str(volume)
    if ":" not in text:
        return ""
    source = text.split(":", 1)[0]
    return source if source.startswith(("/", ".", "~")) else ""


#: Top-level keys a stack file may use. ``x-*`` extension fields are allowed
#: too. Everything else is refused: ``include`` pulls in other compose files
#: from the host, unchecked, and a key added to compose later is unknown here
#: until someone has looked at what it can reach.
_TOP_LEVEL_KEYS = {"version", "name", "services", "networks", "volumes", "secrets", "configs"}

#: Service keys a stack may use. An allowlist, not a denylist: compose keeps
#: adding ways to reach the host (use_api_socket, device_cgroup_rules,
#: volumes_from a container that holds the engine socket), and a denylist only
#: knows the ones someone already thought of. Keys checked further below are
#: marked; the rest cannot reach outside the container's own namespaces.
_SERVICE_KEYS = {
    "image", "command", "entrypoint", "environment", "ports", "expose", "restart",
    "depends_on", "networks", "healthcheck", "labels", "logging", "container_name", "hostname",
    "domainname", "user", "working_dir", "stop_signal", "stop_grace_period", "deploy", "cap_drop",
    "dns", "dns_search", "dns_opt", "extra_hosts", "tmpfs", "ulimits", "shm_size", "mem_limit",
    "memswap_limit", "mem_reservation", "cpus", "cpu_shares", "cpuset", "cpu_count", "cpu_percent",
    "pids_limit", "read_only", "init", "tty", "stdin_open", "platform", "pull_policy", "profiles",
    "group_add", "oom_score_adj", "oom_kill_disable", "mac_address", "links", "attach", "scale",
    "annotations", "gpus",
    # checked below
    "build", "env_file", "volumes", "volumes_from", "devices", "cap_add", "security_opt",
    "privileged", "pid", "ipc", "userns_mode", "cgroup", "uts", "network_mode", "sysctls",
    "secrets", "configs",
}

#: Build keys a stack may use. ssh, entitlements, privileged builds, host
#: networking and local cache import/export all reach the host.
_BUILD_KEYS = {"context", "dockerfile", "dockerfile_inline", "args", "target", "labels",
               "cache_from", "tags", "platforms", "shm_size", "extra_hosts", "no_cache",
               "pull", "additional_contexts", "network"}

#: Devices a stack may pass through, matched on the normalised path: GPUs (DRI
#: render/card nodes, NVIDIA, AMD KFD), TUN for VPNs, and FUSE.
_DEVICE_RE = re.compile(
    r"/dev/dri(/(card|renderD)\d+)?|/dev/nvidia(\d+|ctl|-uvm|-uvm-tools|-modeset)"
    r"|/dev/net/tun|/dev/fuse|/dev/kfd")


def _device_allowed(source: str) -> bool:
    import posixpath
    if ".." in source.split("/") or posixpath.normpath(source) != source.rstrip("/"):
        return False
    return bool(_DEVICE_RE.fullmatch(posixpath.normpath(source)))


#: Values compose reads as true, whatever YAML parser read them first.
_TRUTHY = {"true", "yes", "y", "on", "1"}


def _no_variables(value, where: str) -> None:
    """A $-variable is substituted by compose at deploy time, from the .env or
    the agent's environment, after this check has run: in a field that decides
    what the container can reach, it would let a later value undo the check."""
    if "$" in str(value):
        raise StackError(f"{where}: variables are not allowed here ({value})")


def _flag(value) -> bool:
    return value is True or str(value).strip().lower() in _TRUTHY


def validate_compose(text: str) -> dict:
    if not text or not text.strip():
        raise StackError("the compose file is empty")
    if len(text) > MAX_COMPOSE:
        raise StackError("the compose file is too large")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        err = StackError(f"the compose file is not valid YAML: {exc}")
        mark = getattr(exc, "problem_mark", None)
        err.line = mark.line + 1 if mark is not None else None
        raise err from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise StackError("the compose file needs a services: mapping with at least one service")
    for key in doc:
        if key not in _TOP_LEVEL_KEYS and not str(key).startswith("x-"):
            raise StackError(f"top-level key {key!r} is not allowed in a Vigil-managed stack")

    service_names = set(doc["services"])
    for name, svc in doc["services"].items():
        where = f"service {name!r}"
        if not isinstance(svc, dict):
            raise StackError(f"{where} must be a mapping")
        for key in svc:
            if key not in _SERVICE_KEYS and not str(key).startswith("x-"):
                raise StackError(f"{where}: {key!r} is not allowed in a Vigil-managed stack")

        if "privileged" in svc:
            _no_variables(svc["privileged"], f"{where}: privileged")
            if _flag(svc["privileged"]):
                raise StackError(f"{where}: privileged containers are not allowed")
        for key in ("pid", "ipc", "userns_mode", "cgroup", "uts"):
            if key in svc:
                value = str(svc[key] or "")
                _no_variables(value, f"{where}: {key}")
                if value == "host" or value.startswith("container:"):
                    raise StackError(f"{where}: {key}: {value} is not allowed")
        if "network_mode" in svc:
            mode = str(svc["network_mode"] or "")
            _no_variables(mode, f"{where}: network_mode")
            if mode.startswith("service:") and mode.split(":", 1)[1] not in service_names:
                raise StackError(f"{where}: network_mode {mode} names a service outside this stack")
        for cap in svc.get("cap_add") or []:
            _no_variables(cap, f"{where}: cap_add")
            normal = str(cap).upper().removeprefix("CAP_")
            if normal in _FORBIDDEN_CAPS:
                raise StackError(f"{where}: cap_add: {normal} is not allowed")
        for opt in svc.get("security_opt") or []:
            _no_variables(opt, f"{where}: security_opt")
            low = str(opt).lower().replace("=", ":")
            if "unconfined" in low or low in ("label:disable", "no-new-privileges:false"):
                raise StackError(f"{where}: security_opt {opt} is not allowed")
        for device in svc.get("devices") or []:
            source = (str(device.get("source") or "") if isinstance(device, dict)
                      else str(device).split(":", 1)[0])
            _no_variables(source, f"{where}: devices")
            if not _device_allowed(source):
                raise StackError(f"{where}: passing through the host device {source} is not allowed")
        for key, value in (svc.get("sysctls") or {}).items() if isinstance(svc.get("sysctls"), dict) else []:
            if not str(key).startswith("net."):
                raise StackError(f"{where}: sysctl {key} is not allowed (only net.*)")
        for entry in svc.get("sysctls") or [] if isinstance(svc.get("sysctls"), list) else []:
            if not str(entry).startswith("net."):
                raise StackError(f"{where}: sysctl {entry} is not allowed (only net.*)")
        for source in svc.get("volumes_from") or []:
            _no_variables(source, f"{where}: volumes_from")
            target = str(source).split(":", 1)[0]
            if str(source).startswith("container:") or target not in service_names:
                raise StackError(f"{where}: volumes_from may only name a service in this stack ({source})")

        env_files = svc.get("env_file") or []
        for env_file in env_files if isinstance(env_files, list) else [env_files]:
            path = env_file.get("path") if isinstance(env_file, dict) else env_file
            _no_variables(path, f"{where}: env_file")
            _local_path_problem(path, "env_file", where)
        build = svc.get("build")
        if build is not None:
            if not isinstance(build, dict):
                build = {"context": build}
            for key in build:
                if key not in _BUILD_KEYS:
                    raise StackError(f"{where}: build.{key} is not allowed in a Vigil-managed stack")
            for key in ("context", "dockerfile"):
                if build.get(key) is not None:
                    _no_variables(build[key], f"{where}: build.{key}")
                    _local_path_problem(build[key], f"build.{key}", where)
            if str(build.get("network") or "") not in ("", "default", "none"):
                raise StackError(f"{where}: build.network {build['network']} is not allowed")
            for ref in build.get("cache_from") or []:
                text_ref = str(ref)
                _no_variables(text_ref, f"{where}: build.cache_from")
                if "type=local" in text_ref.replace(" ", ""):
                    raise StackError(f"{where}: build.cache_from may not read a local path ({ref})")
            contexts = build.get("additional_contexts") or {}
            for ctx in (contexts.values() if isinstance(contexts, dict) else contexts):
                text_ctx = str(ctx).split("=", 1)[-1] if not isinstance(contexts, dict) else str(ctx)
                _no_variables(text_ctx, f"{where}: build.additional_contexts")
                if text_ctx.startswith(("docker-image://", "service:", "https://", "git@")):
                    continue
                _local_path_problem(text_ctx, "build.additional_contexts", where)

        for volume in svc.get("volumes") or []:
            if isinstance(volume, dict):
                _no_variables(volume.get("source") or "", f"{where}: volumes")
            else:
                _no_variables(str(volume).split(":", 1)[0], f"{where}: volumes")
            source = _bind_source(volume)
            if not source:
                continue
            if source.startswith("~"):
                raise StackError(f"{where}: binding a home directory ({source}) is not allowed")
            parts = source.replace("\\", "/").split("/")
            if ".." in parts:
                raise StackError(f"{where}: a bind source may not climb out with '..' ({source})")
            if source.startswith("/") and (source.rstrip("/") == "" or any(
                    source == p or source.startswith(p + "/") for p in _FORBIDDEN_BINDS)):
                raise StackError(f"{where}: binding {source} from the host is not allowed")

    # Top-level named volumes can bind a host path through driver_opts, and
    # secrets/configs can read a host file into the container.
    for vname, vol in (doc.get("volumes") or {}).items():
        opts = (vol or {}).get("driver_opts") if isinstance(vol, dict) else None
        for opt_value in (opts or {}).values():
            _no_variables(opt_value, f"volume {vname!r}: driver_opts")
        device = str((opts or {}).get("device") or "")
        if device and (device.startswith(("/", "~")) or ".." in device.split("/")):
            raise StackError(f"volume {vname!r}: binding the host path {device} is not allowed")
    for section in ("secrets", "configs"):
        for item, spec in (doc.get(section) or {}).items():
            if isinstance(spec, dict) and "file" in spec:
                _no_variables(spec["file"], f"{section[:-1]} {item!r}")
                _local_path_problem(spec["file"], "file", f"{section[:-1]} {item!r}")
    return doc


def parse_env(text: str) -> list[tuple[str, str]]:
    """``KEY=value`` lines (comments and blanks allowed), refused otherwise."""
    if len(text or "") > MAX_ENV:
        raise StackError("the .env is too large")
    pairs = []
    for n, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not ENV_KEY_RE.match(key):
            raise StackError(f".env line {n}: expected KEY=value")
        pairs.append((key, value))
    return pairs


def render_env(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"{k}={v}\n" for k, v in pairs)
