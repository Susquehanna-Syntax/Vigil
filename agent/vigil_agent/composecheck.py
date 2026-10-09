"""Check what compose will actually run, just before it runs it.

The server validates the compose text it is sent (apps/stacks/validation.py),
but compose re-reads that text: it substitutes variables from the .env and
the environment, merges include/extends, normalises paths and booleans. Any
difference between the two readings is a way past the server's check. So the
agent asks compose for its resolved configuration (``config --format json``)
and checks that, the same rules applied to exactly what ``up`` will start.
"""
from __future__ import annotations

import posixpath
import re

FORBIDDEN_BINDS = ("/run", "/var/run", "/etc", "/root", "/boot", "/proc", "/sys", "/dev", "/usr",
                   "/bin", "/sbin", "/lib", "/lib64", "/var/lib/docker", "/var/lib/containers",
                   "/var/lib/containerd", "/var/lib/vigil-agent", "/var/spool/cron", "/opt/vigil")
FORBIDDEN_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "SYS_BOOT",
                  "DAC_READ_SEARCH", "BPF", "PERFMON", "MAC_ADMIN", "MAC_OVERRIDE"}
DEVICE_RE = re.compile(r"/dev/dri(/(card|renderD)\d+)?|/dev/nvidia(\d+|ctl|-uvm|-uvm-tools|-modeset)"
                       r"|/dev/net/tun|/dev/fuse|/dev/kfd")
REFUSED_SERVICE_KEYS = ("use_api_socket", "device_cgroup_rules", "post_start", "pre_stop", "develop",
                        "runtime", "annotations", "cgroup_parent")


def _norm(path: str) -> str:
    return "/" + posixpath.normpath(str(path)).lstrip("/")


def bind_forbidden(source: str) -> bool:
    """Is, is inside, or contains a protected path."""
    path = _norm(source)
    if path == "/":
        return True
    return any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in FORBIDDEN_BINDS)


def _inside(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root + "/")


def problems(config: dict, workdir: str) -> list[str]:
    """Every reason not to start this resolved configuration ([] = fine)."""
    out: list[str] = []
    services = config.get("services") or {}
    names = set(services)
    for name, svc in services.items():
        where = f"service {name!r}"
        for key in REFUSED_SERVICE_KEYS:
            if svc.get(key):
                out.append(f"{where}: {key} is not allowed")
        if svc.get("privileged") is True:
            out.append(f"{where}: privileged")
        for key in ("pid", "ipc", "userns_mode", "cgroup", "uts"):
            value = str(svc.get(key) or "").strip().lower()
            if value == "host" or value.startswith("container:"):
                out.append(f"{where}: {key}: {value}")
        for cap in svc.get("cap_add") or []:
            if str(cap).upper().removeprefix("CAP_") in FORBIDDEN_CAPS:
                out.append(f"{where}: cap_add {cap}")
        for opt in svc.get("security_opt") or []:
            low = str(opt).lower().replace("=", ":")
            if "unconfined" in low or low in ("label:disable", "no-new-privileges:false"):
                out.append(f"{where}: security_opt {opt}")
        for device in svc.get("devices") or []:
            source = device.get("source", "") if isinstance(device, dict) else str(device).split(":", 1)[0]
            if not DEVICE_RE.fullmatch(_norm(source)) or ".." in str(source).split("/"):
                out.append(f"{where}: device {source}")
        for key in (svc.get("sysctls") or {}):
            if not str(key).startswith("net."):
                out.append(f"{where}: sysctl {key}")
        for ref in svc.get("volumes_from") or []:
            if str(ref).startswith("container:") or str(ref).split(":", 1)[0] not in names:
                out.append(f"{where}: volumes_from {ref}")
        for vol in svc.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("type") == "bind" and bind_forbidden(vol.get("source", "")):
                out.append(f"{where}: bind of {vol.get('source')}")
        build = svc.get("build") or {}
        if isinstance(build, dict):
            if build.get("context") and not str(build["context"]).startswith(("http", "git@")) \
                    and not _inside(build["context"], workdir):
                out.append(f"{where}: build context {build['context']} is outside the stack")
            for key in ("ssh", "entitlements", "privileged"):
                if build.get(key):
                    out.append(f"{where}: build.{key}")
            if str(build.get("network") or "") not in ("", "default", "none"):
                out.append(f"{where}: build.network {build['network']}")
    for vname, vol in (config.get("volumes") or {}).items():
        device = str(((vol or {}).get("driver_opts") or {}).get("device") or "")
        if device.startswith("/") or ".." in device.split("/"):
            out.append(f"volume {vname!r}: binds the host path {device}")
    for section in ("secrets", "configs"):
        for item, spec in (config.get(section) or {}).items():
            path = (spec or {}).get("file")
            if path and not _inside(path, workdir):
                out.append(f"{section[:-1]} {item!r}: {path} is outside the stack")
    return out
