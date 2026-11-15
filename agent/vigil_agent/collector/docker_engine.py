"""The Docker engine's Unix-socket HTTP transport, shared by the three modules
that talk to the daemon without the SDK: ``docker_updates``, ``docker_resources``
and ``docker_containers``.

``_DOCKER_SOCKET`` lives here once because all three ask the same daemon;
``_engine_available``/``_docker_api_get`` are their one way in, so a caller that
wants to swap the engine out (the suite's fake engine, or a test that says "no
engine here") replaces them on the collector package and each module asks through
``_pkg_attr`` rather than holding its own reference. The engine module itself is
imported inside the two functions: an agent action can reach the collector
through ``executor``, which imports the actions package, whose modules import the
collector back — a top-level import would be evaluated mid-cycle.
"""

import http.client as _http_client
import json as _json

from ._common import shared

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
    overridden = shared("_docker_api_get")
    if overridden is not None and overridden is not _docker_api_get:
        return overridden(path)
    from .. import engine

    client = engine.default_client()
    if client is None:
        return None
    status, body, _headers = client.raw("GET", path, versioned=False)
    if status == 200:
        return _json.loads(body)
    return None


def _engine_available() -> bool:
    overridden = shared("_engine_available")
    if overridden is not None and overridden is not _engine_available:
        return bool(overridden())
    from .. import engine

    return engine.default_client() is not None
