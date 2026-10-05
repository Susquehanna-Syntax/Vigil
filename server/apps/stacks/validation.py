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
        if any(str(c).upper() == "ALL" for c in svc.get("cap_add") or []):
            raise StackError(f"service {name!r}: cap_add: ALL is not allowed")
        for volume in svc.get("volumes") or []:
            source = _bind_source(volume)
            if not source:
                continue
            parts = source.replace("\\", "/").split("/")
            if ".." in parts:
                raise StackError(f"service {name!r}: a bind source may not climb out with '..' ({source})")
            if source.startswith("/") and (source.rstrip("/") == "" or any(
                    source == p or source.startswith(p + "/") for p in _FORBIDDEN_BINDS)):
                raise StackError(f"service {name!r}: binding {source} from the host is not allowed")
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
