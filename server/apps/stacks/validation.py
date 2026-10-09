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
    pass


#: Capabilities that hand a container the host (kernel modules, mounts, other
#: processes' memory, raw devices, LSM policy). NET_ADMIN and friends stay
#: allowed: VPN and network containers need them and they do not escape.
_FORBIDDEN_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "SYS_BOOT",
                   "DAC_READ_SEARCH", "BPF", "PERFMON", "MAC_ADMIN", "MAC_OVERRIDE"}
#: Host devices a stack may pass through: GPUs, a TUN device for VPNs, FUSE.
_ALLOWED_DEVICE_PREFIXES = ("/dev/dri", "/dev/nvidia", "/dev/net/tun", "/dev/fuse", "/dev/kfd")
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


def validate_compose(text: str) -> dict:
    if not text or not text.strip():
        raise StackError("the compose file is empty")
    if len(text) > MAX_COMPOSE:
        raise StackError("the compose file is too large")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StackError(f"the compose file is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise StackError("the compose file needs a services: mapping with at least one service")
    for name, svc in doc["services"].items():
        if not isinstance(svc, dict):
            raise StackError(f"service {name!r} must be a mapping")
        if svc.get("privileged") is True:
            raise StackError(f"service {name!r}: privileged containers are not allowed")
        if str(svc.get("pid") or "") == "host":
            raise StackError(f"service {name!r}: pid: host is not allowed")
        for cap in svc.get("cap_add") or []:
            normal = str(cap).upper().removeprefix("CAP_")
            if normal in _FORBIDDEN_CAPS:
                raise StackError(f"service {name!r}: cap_add: {normal} is not allowed")
        for key in _HOST_NAMESPACE_KEYS:
            if str(svc.get(key) or "") == "host":
                raise StackError(f"service {name!r}: {key}: host is not allowed")
        for opt in svc.get("security_opt") or []:
            low = str(opt).lower().replace("=", ":")
            if "unconfined" in low or low in ("label:disable", "no-new-privileges:false"):
                raise StackError(f"service {name!r}: security_opt {opt} is not allowed")
        for device in svc.get("devices") or []:
            source = (str(device.get("source") or "") if isinstance(device, dict)
                      else str(device).split(":", 1)[0])
            if not source.startswith(_ALLOWED_DEVICE_PREFIXES):
                raise StackError(f"service {name!r}: passing through the host device {source} is not allowed")
        env_files = svc.get("env_file") or []
        for env_file in env_files if isinstance(env_files, list) else [env_files]:
            path = env_file.get("path") if isinstance(env_file, dict) else env_file
            _local_path_problem(path, "env_file", f"service {name!r}")
        build = svc.get("build")
        if build is not None:
            _local_path_problem(build.get("context") if isinstance(build, dict) else build,
                                "the build context", f"service {name!r}")
        for volume in svc.get("volumes") or []:
            source = _bind_source(volume)
            if not source:
                continue
            if source.startswith("~"):
                raise StackError(f"service {name!r}: binding a home directory ({source}) is not allowed")
            parts = source.replace("\\", "/").split("/")
            if ".." in parts:
                raise StackError(f"service {name!r}: a bind source may not climb out with '..' ({source})")
            if source.startswith("/") and (source.rstrip("/") == "" or any(
                    source == p or source.startswith(p + "/") for p in _FORBIDDEN_BINDS)):
                raise StackError(f"service {name!r}: binding {source} from the host is not allowed")
    # Top-level named volumes can bind a host path through driver_opts, and
    # secrets/configs can read a host file into the container.
    for vname, vol in (doc.get("volumes") or {}).items():
        opts = (vol or {}).get("driver_opts") if isinstance(vol, dict) else None
        device = str((opts or {}).get("device") or "")
        if device and (device.startswith(("/", "~")) or ".." in device.split("/")):
            raise StackError(f"volume {vname!r}: binding the host path {device} is not allowed")
    for section in ("secrets", "configs"):
        for item, spec in (doc.get(section) or {}).items():
            if isinstance(spec, dict) and "file" in spec:
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
