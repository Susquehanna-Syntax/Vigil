"""Container engine client — the Docker Engine API over a Unix socket (M11).

One client for Docker and Podman (whose compat API speaks the same
protocol), used for every single-container action and for the inventory.
Stacks go through the compose CLI pointed at the same socket.

Socket discovery, in order: ``$DOCKER_HOST`` (``unix://`` only),
``/var/run/docker.sock``, ``/run/podman/podman.sock`` (rootful Podman), then
each user's rootless Podman socket under ``/run/user/<uid>/podman/``. A
rootless socket belongs to its user: actions on its containers act as that
user's engine, never as root's.

API version: ``/_ping`` proves the engine answers; ``/version`` names it and
gives its ``ApiVersion``, and requests are pinned to the lower of that and
``MAX_API`` so a newer engine is spoken to in a dialect this agent knows.
Never below the engine's reported ``MinAPIVersion`` though: Docker 29 dropped
the old dialects and refuses anything under its floor with a 400.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

#: The newest Engine API this agent is written against.
MAX_API = "1.47"
DEFAULT_TIMEOUT = 30

_ROOTFUL = ("/var/run/docker.sock", "/run/podman/podman.sock")
#: The monitor-mode proxy's socket (engine_proxy.PROXY_SOCKET): read-only.
_READ_ONLY = "/run/vigil/engine-ro.sock"
_ROOTLESS_GLOB = "/run/user/*/podman/podman.sock"


class EngineError(RuntimeError):
    """The engine refused or could not be reached. ``status`` is the HTTP
    status (0 when the socket itself failed)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _api_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


def discover_sockets(env=None, root: str = "/") -> list[dict]:
    """Every engine socket on this host: ``[{"path", "rootless", "uid"}]``."""
    env = os.environ if env is None else env
    base = Path(root)
    found: list[dict] = []
    seen: set[str] = set()

    def add(path: str, rootless: bool = False, uid: int | None = None) -> None:
        full = base / path.lstrip("/")
        if str(full) in seen or not full.exists():
            return
        seen.add(str(full))
        found.append({"path": str(full), "rootless": rootless, "uid": uid})

    # An unprivileged agent cannot open the engine's own socket, so the
    # read-only proxy comes first when it is there; root skips nothing by it.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        add(_READ_ONLY)
    docker_host = str(env.get("DOCKER_HOST") or "")
    if docker_host.startswith("unix://"):
        add(docker_host[len("unix://"):])
    for path in _ROOTFUL:
        add(path)
    for sock in sorted(base.glob(_ROOTLESS_GLOB.lstrip("/"))):
        try:
            uid = int(sock.parts[-3])
        except ValueError:
            continue
        add("/" + str(sock.relative_to(base)), rootless=True, uid=uid)
    return found


class EngineClient:
    """A Docker Engine API client bound to one socket."""

    def __init__(self, socket_path: str, *, rootless: bool = False, uid: int | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.socket_path = socket_path
        self.rootless = rootless
        self.uid = uid
        self.timeout = timeout
        self._api: str | None = None
        self._version: dict | None = None

    # ── transport ───────────────────────────────────────────────────────
    def raw(self, method: str, path: str, *, query: dict | None = None, body=None,
            headers: dict | None = None, versioned: bool = True,
            timeout: float | None = None) -> tuple[int, bytes, dict]:
        """One request; returns (status, body bytes, headers)."""
        if versioned:
            path = f"/v{self.api_version()}{path}"
        if query:
            path = f"{path}?{urlencode({k: v for k, v in query.items() if v is not None})}"
        payload = None
        hdrs = {"Host": "localhost", **(headers or {})}
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn = _UnixConnection(self.socket_path, timeout or self.timeout)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.getheaders()}
        except OSError as exc:
            raise EngineError(f"engine socket {self.socket_path}: {exc}") from exc
        finally:
            conn.close()

    def request(self, method: str, path: str, *, ok=(200, 201, 204, 304), **kw):
        status, data, _ = self.raw(method, path, **kw)
        if status not in ok:
            message = data.decode("utf-8", "replace").strip()
            try:
                message = json.loads(message).get("message") or message
            except (ValueError, AttributeError):
                pass
            raise EngineError(f"{method} {path.split('?')[0]}: {status} {message}"[:500], status)
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError:
            return data

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    # ── identity ────────────────────────────────────────────────────────
    def ping(self) -> bool:
        try:
            status, _data, _ = self.raw("GET", "/_ping", versioned=False, timeout=5)
        except EngineError:
            return False
        return status == 200

    def version(self) -> dict:
        if self._version is None:
            self._version = self.request("GET", "/version", versioned=False) or {}
        return self._version

    def api_version(self) -> str:
        if self._api is None:
            info = self.version()
            server = str(info.get("ApiVersion") or "1.41")
            chosen = min(server, MAX_API, key=_api_tuple)
            # An engine that dropped old dialects (Docker 29: minimum 1.44) refuses a
            # request pinned below its floor, so never go under what it accepts.
            floor = str(info.get("MinAPIVersion") or "")
            if floor and _api_tuple(floor) > _api_tuple(chosen):
                chosen = floor
            self._api = chosen
        return self._api

    def kind(self) -> str:
        """docker or podman."""
        info = self.version()
        names = [str((info.get("Platform") or {}).get("Name") or "")]
        names += [str(c.get("Name") or "") for c in info.get("Components") or []
                  if isinstance(c, dict)]
        return "podman" if any("podman" in n.lower() for n in names) else "docker"

    def describe(self) -> dict:
        """What the check-in reports: ``{kind, version, api_version, rootless}``."""
        return {"kind": self.kind(), "version": str(self.version().get("Version") or ""),
                "api_version": self.api_version(), "rootless": self.rootless,
                **({"uid": self.uid} if self.uid is not None else {})}


def clients(env=None, root: str = "/") -> list[EngineClient]:
    """A client for every socket that answers /_ping."""
    out = []
    for sock in discover_sockets(env, root):
        client = EngineClient(sock["path"], rootless=sock["rootless"], uid=sock["uid"])
        if client.ping():
            out.append(client)
    return out


def default_client(env=None, root: str = "/") -> EngineClient | None:
    """The first answering engine — rootful before rootless."""
    found = clients(env, root)
    return found[0] if found else None


def engine_report(env=None, root: str = "/") -> list[dict] | None:
    """Every answering engine, described for the check-in; None when there is
    no engine at all (the server then keeps what it had)."""
    found = []
    for client in clients(env, root):
        try:
            found.append(client.describe())
        except EngineError as exc:
            logger.debug("engine %s did not describe itself: %s", client.socket_path, exc)
    return found or None
