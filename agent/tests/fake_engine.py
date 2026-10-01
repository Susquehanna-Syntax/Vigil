"""A fake container engine on a real Unix socket, for engine client tests.

Routes are ``{(method, path_without_query): (status, body)}``; ``body`` may
be a callable taking (path, json_body) and returning (status, body). Every
request is recorded as (method, full path, json body).
"""
import json
import os
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

DOCKER_VERSION = {"Version": "27.3.1", "ApiVersion": "1.47",
                  "Platform": {"Name": "Docker Engine - Community"}, "Components": []}
PODMAN_VERSION = {"Version": "5.2.2", "ApiVersion": "1.41",
                  "Platform": {"Name": "linux/amd64"},
                  "Components": [{"Name": "Podman Engine", "Version": "5.2.2"}]}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeEngine:
    def __init__(self, version=None, routes=None, path=None):
        self.version = DOCKER_VERSION if version is None else version
        self.routes = dict(routes or {})
        self.requests = []
        self._dir = tempfile.TemporaryDirectory()
        self.path = path or os.path.join(self._dir.name, "engine.sock")
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def address_string(self):
                return "unix"

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                engine.requests.append((method, self.path, body))
                bare = self.path.split("?", 1)[0]
                if bare == "/_ping":
                    status, out = 200, "OK"
                elif bare == "/version":
                    status, out = 200, engine.version
                else:
                    stripped = bare.split("/", 2)[2] if bare.startswith("/v1.") else bare.lstrip("/")
                    route = engine.routes.get((method, "/" + stripped))
                    if route is None:
                        status, out = 404, {"message": f"no route {method} /{stripped}"}
                    elif callable(route):
                        status, out = route(self.path, body)
                    else:
                        status, out = route
                data = out if isinstance(out, bytes) else (
                    out.encode() if isinstance(out, str) else json.dumps(out).encode())
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = _Server(self.path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._dir.cleanup()
