"""The Docker engine's Unix-socket HTTP transport, shared by the three modules
that talk to the daemon without the SDK: ``docker_updates``, ``docker_resources``
and ``docker_containers``.

``_DOCKER_SOCKET`` lives here once because all three ask the same daemon, and
``_engine_available``/``_docker_api_get`` are their one way in. The engine module is
imported inside the two functions: an agent action can reach the collector
through ``executor``, which imports the actions package, whose modules import the
collector back — a top-level import would be evaluated mid-cycle.
"""

import http.client as _http_client
import json as _json

_DOCKER_SOCKET = "/var/run/docker.sock"


class _UnixHTTPConnection(_http_client.HTTPConnection):
    """HTTPConnection routed through a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self) -> None:
        import socket as _sock_mod
        self.sock = _sock_mod.socket(_sock_mod.AF_UNIX, _sock_mod.SOCK_STREAM)
        self.sock.connect(self._socket_path)


def _docker_api_get(path: str):
    """GET from the container engine (Docker or Podman, M11). Returns parsed
    JSON, or None when there is no engine or it does not answer 200."""
    from .. import engine

    client = engine.default_client()
    if client is None:
        return None
    status, body, _headers = client.raw("GET", path, versioned=False)
    if status == 200:
        return _json.loads(body)
    return None


def _engine_available() -> bool:
    from .. import engine

    return engine.default_client() is not None
