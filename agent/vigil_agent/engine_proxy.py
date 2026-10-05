"""A read-only container engine socket for monitor mode (M11).

A monitor-mode agent runs unprivileged, and the engine socket is root's —
handing the agent that socket (or the docker group) would hand it root.
Instead ``vigil-agent --engine-proxy`` runs as a small root service that
listens on ``PROXY_SOCKET`` (owned by the agent's user, mode 0600) and
forwards only what the inventory reads: GET of the container list, one
container's inspect / stats / logs, the image list, and the engine's own
ping and version. Everything else — every POST and DELETE, exec, build,
volumes, the socket's raw upgrade paths — is answered 403 and never reaches
the engine.
"""

from __future__ import annotations

import logging
import os
import re
import socketserver
from http.server import BaseHTTPRequestHandler

from .engine import EngineClient, EngineError

logger = logging.getLogger(__name__)

PROXY_SOCKET = "/run/vigil/engine-ro.sock"

_VERSION = r"(?:/v1\.\d+)?"
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
ALLOWED = tuple(re.compile(f"^{_VERSION}{p}$") for p in (
    r"/_ping",
    r"/version",
    r"/info",
    r"/containers/json",
    rf"/containers/{_ID}/json",
    rf"/containers/{_ID}/stats",
    rf"/containers/{_ID}/logs",
    r"/images/json",
))


def allowed(method: str, path: str) -> bool:
    """True only for a GET of one of the read-only paths (query aside)."""
    if method != "GET":
        return False
    bare = path.split("?", 1)[0]
    if "/stats" in bare and "stream=false" not in path and "stream=0" not in path:
        return False   # a streaming stats call would hold the proxy open forever
    if "/logs" in bare and ("follow=true" in path or "follow=1" in path):
        return False
    return any(p.match(bare) for p in ALLOWED)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(upstream: EngineClient, path: str = PROXY_SOCKET, owner_uid: int | None = None):
    """A server forwarding allowed requests to *upstream*; not yet serving."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def address_string(self):
            return "unix"

        def _reply(self, status: int, body: bytes, ctype: str = "application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            if not allowed(self.command, self.path):
                logger.info("engine proxy refused %s %s", self.command, self.path.split("?")[0])
                self._reply(403, b'{"message":"read-only engine socket (Vigil monitor mode)"}')
                return
            try:
                status, body, headers = upstream.raw("GET", self.path, versioned=False)
            except EngineError as exc:
                self._reply(502, f'{{"message":"{exc}"}}'.encode())
                return
            self._reply(status, body, headers.get("content-type", "application/json"))

        do_GET = do_POST = do_DELETE = do_PUT = do_HEAD = _handle

    if os.path.exists(path):
        os.unlink(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    server = _Server(path, Handler)
    os.chmod(path, 0o600)
    if owner_uid is not None:
        os.chown(path, owner_uid, -1)
    return server


def run(owner: str = "vigil-agent") -> int:
    """Entry point for ``vigil-agent --engine-proxy``: serve until stopped."""
    import pwd

    from .engine import discover_sockets

    upstream = next((s for s in discover_sockets() if not s["rootless"]
                     and s["path"] != PROXY_SOCKET), None)
    if upstream is None:
        logger.error("engine proxy: no rootful engine socket on this host")
        return 1
    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError:
        uid = None
    server = make_server(EngineClient(upstream["path"]), owner_uid=uid)
    logger.info("engine proxy: %s → %s (read-only)", PROXY_SOCKET, upstream["path"])
    server.serve_forever()
    return 0
