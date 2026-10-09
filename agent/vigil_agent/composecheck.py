"""Check what compose will actually run, just before it runs it.

The server validates the compose text it is sent (apps/stacks/validation.py),
but compose re-reads that text: it substitutes variables from the .env and
the environment, merges include/extends, normalises paths and booleans. Any
difference between the two readings is a way past the server's check. So the
agent asks compose for its resolved configuration (``config --format json``),
checks that, and then starts *that file* — the checked bytes are the ones
``up`` reads (stacks._stack_deploy).

The rules accept every shape compose allows, not just the one docker prints:
podman-compose's ``config`` hands back the file much as it was written (short
volume strings, list sysctls, "true" strings). A shape not understood here is
refused, and so is any service or top-level key not on the allowlist.
"""
from __future__ import annotations

import os
import posixpath
import re

FORBIDDEN_BINDS = ("/run", "/var/run", "/etc", "/root", "/boot", "/proc", "/sys", "/dev", "/usr",
                   "/bin", "/sbin", "/lib", "/lib64", "/var/lib/docker", "/var/lib/containers",
                   "/var/lib/containerd", "/var/lib/vigil-agent", "/var/spool/cron", "/opt/vigil")
FORBIDDEN_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "SYS_BOOT",
                  "DAC_READ_SEARCH", "BPF", "PERFMON", "MAC_ADMIN", "MAC_OVERRIDE"}
DEVICE_RE = re.compile(r"/dev/dri(/(card|renderD)\d+)?|/dev/nvidia(\d+|ctl|-uvm|-uvm-tools|-modeset)"
                       r"|/dev/net/tun|/dev/fuse|/dev/kfd")

#: The same allowlist as the server's validation.py, less what it refuses.
#: A key compose adds later is refused here until someone has looked at it.
SERVICE_KEYS = {
    "image", "command", "entrypoint", "environment", "ports", "expose", "restart",
    "depends_on", "networks", "healthcheck", "labels", "logging", "container_name", "hostname",
    "domainname", "user", "working_dir", "stop_signal", "stop_grace_period", "deploy", "cap_drop",
    "dns", "dns_search", "dns_opt", "extra_hosts", "tmpfs", "ulimits", "shm_size", "mem_limit",
    "memswap_limit", "mem_reservation", "cpus", "cpu_shares", "cpuset", "cpu_count", "cpu_percent",
    "pids_limit", "read_only", "init", "tty", "stdin_open", "platform", "pull_policy", "profiles",
    "group_add", "oom_score_adj", "oom_kill_disable", "mac_address", "links", "attach", "scale", "gpus",
    # checked below
    "build", "env_file", "volumes", "volumes_from", "devices", "cap_add", "security_opt",
    "privileged", "pid", "ipc", "userns_mode", "cgroup", "uts", "network_mode", "sysctls",
    "secrets", "configs",
}
TOP_LEVEL_KEYS = {"version", "name", "services", "networks", "volumes", "secrets", "configs"}
_TRUTHY = {"true", "yes", "y", "on", "1"}
_REMOTE_CONTEXT = ("http://", "https://", "git://", "git@", "ssh://")


def _norm(path: str) -> str:
    return "/" + posixpath.normpath(str(path)).lstrip("/")


def _forbidden(path: str) -> bool:
    path = _norm(path)
    if path == "/":
        return True
    return any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in FORBIDDEN_BINDS)


def bind_forbidden(source: str) -> bool:
    """Is, is inside, or contains a protected path — as written, and once
    symlinks on the host are followed (a container bound to its own stack
    folder can plant ``data -> /`` there before the next deploy)."""
    return _forbidden(source) or _forbidden(os.path.realpath(_norm(source)))


def _inside(path: str, root: str) -> bool:
    root = _norm(root)
    return all(p == root or p.startswith(root + "/")
               for p in (_norm(path), os.path.realpath(_norm(path))))


def _flag(value) -> bool:
    return value is True or str(value).strip().lower() in _TRUTHY


def _items(value) -> list:
    """A list as given, a lone string as a one-item list, nothing as []."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _has_var(value) -> bool:
    # In docker's output a literal $ is escaped as $$; podman-compose may hand
    # back an unsubstituted one. Either way the file is read again by `up`, so
    # a $ in a field that decides access could still change after this check.
    return "$" in str(value)


def _abs(path: str, workdir: str) -> str:
    path = str(path)
    # "~" is the agent's home — never inside a stack, so leave it to be refused
    return path if path.startswith(("/", "~")) else posixpath.normpath(posixpath.join(workdir, path))


def _bind_problem(source: str, workdir: str) -> str | None:
    if source.startswith("~"):
        return f"bind of a home directory ({source})"
    path = _abs(source, workdir)
    link = _symlink_in(path, workdir)
    if link:
        return f"bind of {source}: {link} is a symlink"
    if _inside(path, workdir):                           # the stack's own folder, symlinks followed
        return None
    return f"bind of {source}" if bind_forbidden(path) else None


def _symlink_in(path: str, workdir: str) -> str | None:
    """The first symlink on the way from the stack folder down to *path*. A
    container that can write the stack folder can plant one; the engine follows
    it when it mounts, after any check here — so a link already there is
    refused, and _covered() refuses a bind a running container could re-link."""
    path, root = _norm(path), _norm(workdir)
    if not path.startswith(root + "/"):
        return None
    current = root
    for part in path[len(root) + 1:].split("/"):
        current = posixpath.join(current, part)
        if os.path.islink(current):
            return current
    return None


def _volume_problem(vol, workdir: str) -> str | None:
    if isinstance(vol, str):
        parts = vol.split(":")
        if len(parts) == 1:
            return None                                 # anonymous volume: "/data"
        source = parts[0]
        if _has_var(source):
            return f"volume source {source} has a variable"
        if source.startswith(("/", ".", "~")):
            return _bind_problem(source, workdir)
        return None                                      # a named volume
    if not isinstance(vol, dict):
        return f"volume {vol!r} is not understood"
    kind = vol.get("type") or "volume"
    source = str(vol.get("source") or "")
    if _has_var(source):
        return f"volume source {source} has a variable"
    if kind == "bind" or source.startswith(("/", ".", "~")):
        if not source:
            return "bind without a source"
        return _bind_problem(source, workdir)
    if kind not in ("volume", "tmpfs", "image"):
        return f"volume type {kind}"
    return None


def _service(name: str, svc, names: set, workdir: str) -> list[str]:
    where = f"service {name!r}"
    if not isinstance(svc, dict):
        return [f"{where} is not a mapping"]
    out: list[str] = []
    for key, value in svc.items():
        if key not in SERVICE_KEYS and not str(key).startswith("x-") and value not in (None, "", [], {}):
            out.append(f"{where}: {key} is not allowed")
    if "privileged" in svc and (_has_var(svc["privileged"]) or _flag(svc["privileged"])):
        out.append(f"{where}: privileged")
    for key in ("pid", "ipc", "userns_mode", "cgroup", "uts", "network_mode"):
        value = str(svc.get(key) or "").strip().lower()
        if _has_var(value) or value.startswith("container:") or (value == "host" and key != "network_mode"):
            out.append(f"{where}: {key}: {value}")
        if key == "network_mode" and value.startswith("service:") and value.split(":", 1)[1] not in names:
            out.append(f"{where}: network_mode {value}")
    for cap in _items(svc.get("cap_add")):
        if _has_var(cap) or str(cap).strip().upper().removeprefix("CAP_") in FORBIDDEN_CAPS:
            out.append(f"{where}: cap_add {cap}")
    for opt in _items(svc.get("security_opt")):
        low = str(opt).strip().lower().replace("=", ":")
        allowed = (low in ("no-new-privileges", "no-new-privileges:true")
                   or (low.startswith("apparmor:") and "unconfined" not in low)
                   or low.startswith("label:level:"))
        if _has_var(opt) or not allowed:
            out.append(f"{where}: security_opt {opt}")
    for device in _items(svc.get("devices")):
        source = str(device.get("source") or "") if isinstance(device, dict) else str(device).split(":", 1)[0]
        if _has_var(source) or ".." in source.split("/") or not DEVICE_RE.fullmatch(_norm(source)) \
                or not DEVICE_RE.fullmatch(os.path.realpath(_norm(source))):
            out.append(f"{where}: device {source}")
    sysctls = svc.get("sysctls") or {}
    keys = sysctls.keys() if isinstance(sysctls, dict) else [str(s).split("=", 1)[0] for s in _items(sysctls)]
    for key in keys:
        if _has_var(key) or not str(key).strip().startswith("net."):
            out.append(f"{where}: sysctl {key}")
    for ref in _items(svc.get("volumes_from")):
        target = str(ref).removeprefix("service:").split(":", 1)[0]
        if str(ref).startswith("container:") or target not in names:
            out.append(f"{where}: volumes_from {ref}")
    for vol in _items(svc.get("volumes")):
        problem = _volume_problem(vol, workdir)
        if problem:
            out.append(f"{where}: {problem}")
    for env_file in _items(svc.get("env_file")):
        path = env_file.get("path") if isinstance(env_file, dict) else env_file
        if path and not _inside(_abs(path, workdir), workdir):
            out.append(f"{where}: env_file {path} is outside the stack")
    out += _build(where, svc.get("build"), workdir)
    return out


def _local_context(value) -> bool:
    """A path on this host, not a remote repository, an image or another service."""
    return not str(value).startswith((*_REMOTE_CONTEXT, "docker-image://", "service:"))


def _build(where: str, build, workdir: str) -> list[str]:
    if build is None:
        return []
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        return [f"{where}: build is not understood"]
    out = []
    context = build.get("context") or "."
    extra = build.get("additional_contexts") or {}
    if isinstance(extra, list):                         # ["name=path", ...]
        extra = dict(str(e).split("=", 1) if "=" in str(e) else (str(e), "/") for e in extra)
    if not isinstance(extra, dict):
        return [f"{where}: build.additional_contexts is not understood"]
    paths = [("context", context), *((f"additional_contexts.{k}", v) for k, v in extra.items())]
    for label, value in paths:
        if _has_var(value):
            out.append(f"{where}: build.{label} has a variable")
        elif _local_context(value) and not _inside(_abs(value, workdir), workdir):
            out.append(f"{where}: build.{label} {value} is outside the stack")
    dockerfile = build.get("dockerfile")
    if dockerfile and _local_context(context):
        base = _abs(context, workdir)
        if _has_var(dockerfile) or not _inside(_abs(dockerfile, base), workdir):
            out.append(f"{where}: build.dockerfile {dockerfile} is outside the stack")
    for key in ("ssh", "entitlements", "privileged"):
        if build.get(key):
            out.append(f"{where}: build.{key}")
    if str(build.get("network") or "") not in ("", "default", "none"):
        out.append(f"{where}: build.network {build['network']}")
    return out


#: Fields whose strings only reach inside the container: a $ there may stay
#: ($$ in docker's output re-reads as a literal $). Anywhere else a $ is
#: refused — compose reads the checked file again, and could substitute it.
INERT_KEYS = {"environment", "command", "entrypoint", "healthcheck", "labels", "content"}


def _variables(node, where: str, out: list[str], depth: int = 0) -> None:
    if depth > 64:
        out.append(f"{where}: nested too deeply")
    elif isinstance(node, dict):
        for key, value in node.items():
            if _has_var(key):
                out.append(f"{where}: key {key!r} has a variable")
            elif key not in INERT_KEYS and not str(key).startswith("x-"):
                _variables(value, f"{where}.{key}", out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _variables(value, where, out, depth + 1)
    elif _has_var(node):
        out.append(f"{where}: {node!r} has a variable")


def _pin_volume(vol, workdir: str):
    if isinstance(vol, str):
        parts = vol.split(":")
        if len(parts) > 1 and parts[0].startswith("."):
            return ":".join([_abs(parts[0], workdir), *parts[1:]])
        return vol
    if isinstance(vol, dict) and str(vol.get("source") or "").startswith("."):
        return {**vol, "source": _abs(vol["source"], workdir)}
    return vol


def pin(config: dict, workdir: str) -> dict:
    """The configuration with every relative host path made absolute against
    the stack folder. docker's output already is; podman-compose's may not be,
    and ``up`` reads the checked file from the agent's data dir, where compose
    would resolve "./" and "../" against *that* folder instead. What is
    checked and what is started is this pinned copy."""
    import copy

    config = copy.deepcopy(config)
    if not isinstance(config, dict):
        return config
    for svc in (config.get("services") or {}).values() if isinstance(config.get("services"), dict) else ():
        if not isinstance(svc, dict):
            continue
        if isinstance(svc.get("volumes"), list):
            svc["volumes"] = [_pin_volume(v, workdir) for v in svc["volumes"]]
        if "env_file" in svc:
            svc["env_file"] = [{**e, "path": _abs(e["path"], workdir)} if isinstance(e, dict) and e.get("path")
                               else _abs(e, workdir) if isinstance(e, str) else e
                               for e in _items(svc["env_file"])]
        build = svc.get("build")
        if isinstance(build, str):
            build = svc["build"] = {"context": build}
        if isinstance(build, dict):
            context = build.get("context") or "."
            if _local_context(context):
                build["context"] = _abs(context, workdir)
            extra = build.get("additional_contexts")
            if isinstance(extra, dict):
                build["additional_contexts"] = {k: _abs(v, workdir) if _local_context(v) else v
                                                for k, v in extra.items()}
    for section in ("secrets", "configs"):
        for spec in (config.get(section) or {}).values() if isinstance(config.get(section), dict) else ():
            if isinstance(spec, dict) and spec.get("file"):
                spec["file"] = _abs(spec["file"], workdir)
    return config


def rw_binds(config: dict, workdir: str) -> list[str]:
    """Host paths this configuration mounts writable."""
    out = []
    for svc in (config.get("services") or {}).values():
        for vol in _items((svc or {}).get("volumes")) if isinstance(svc, dict) else ():
            if isinstance(vol, str):
                parts = vol.split(":")                   # source:target[:options]
                options = parts[2].split(",") if len(parts) > 2 else []
                if len(parts) > 1 and parts[0].startswith(("/", ".")) and "ro" not in options:
                    out.append(_norm(_abs(parts[0], workdir)))
            elif isinstance(vol, dict) and str(vol.get("source") or "").startswith(("/", ".")) \
                    and not _flag(vol.get("read_only")):
                out.append(_norm(_abs(vol["source"], workdir)))
    return out


def _sources(config: dict, workdir: str) -> list[str]:
    out = []
    for svc in (config.get("services") or {}).values():
        for vol in _items((svc or {}).get("volumes")) if isinstance(svc, dict) else ():
            source = vol.split(":")[0] if isinstance(vol, str) and ":" in vol \
                else str(vol.get("source") or "") if isinstance(vol, dict) else ""
            if source.startswith(("/", ".")):
                out.append(_norm(_abs(source, workdir)))
    return out


def _covered(config: dict, workdir: str, live_rw: tuple) -> list[str]:
    """A bind inside a folder some Vigil stack's container can write: that
    container can swap in a symlink between this check and the mount."""
    writable = set(rw_binds(config, workdir)) | {_norm(p) for p in live_rw}
    out = []
    for source in sorted(set(_sources(config, workdir))):
        for real in {source, os.path.realpath(source)}:
            above = [w for w in writable if real.startswith(w.rstrip("/") + "/")]
            if above:
                out.append(f"bind of {source} is inside {above[0]}, which a container can write")
                break
    return out


def problems(config: dict, workdir: str, live_rw: tuple = ()) -> list[str]:
    """Every reason not to start this (pinned) resolved configuration ([] =
    fine). *live_rw*: host paths running Vigil-stack containers mount writable."""
    if not isinstance(config, dict):
        return ["the configuration is not a mapping"]
    out: list[str] = []
    _variables(config, "config", out)
    out += _covered(config, workdir, live_rw)
    for key, value in config.items():
        if key not in TOP_LEVEL_KEYS and not str(key).startswith("x-") and value not in (None, "", [], {}):
            out.append(f"top-level {key} is not allowed")
    services = config.get("services") or {}
    if not isinstance(services, dict):
        return out + ["services is not a mapping"]
    names = set(services)
    for name, svc in services.items():
        out += _service(name, svc, names, workdir)
    for vname, vol in (config.get("volumes") or {}).items():
        opts = (vol or {}).get("driver_opts") if isinstance(vol, dict) else None
        if not isinstance(opts, dict):
            continue
        device = str(opts.get("device") or "")
        kind = str(opts.get("type") or "").lower()
        if any(_has_var(v) for v in opts.values()) or "bind" in str(opts.get("o") or "").lower() \
                or (device and kind not in ("tmpfs", "nfs", "nfs4", "cifs")):
            out.append(f"volume {vname!r}: driver_opts {opts}")
    for section in ("secrets", "configs"):
        for item, spec in (config.get(section) or {}).items():
            path = (spec or {}).get("file") if isinstance(spec, dict) else None
            if path and (_has_var(path) or not _inside(_abs(path, workdir), workdir)):
                out.append(f"{section[:-1]} {item!r}: {path} is outside the stack")
    return out
