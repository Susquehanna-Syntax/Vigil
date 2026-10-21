"""The relay container's entry point: `python -m vigil_relay`."""

import logging
import ssl
import sys

from aiohttp import web

from . import __version__
from .app import make_app
from .config import Config, from_env


def banner(cfg: Config) -> str:
    """The one line the container logs at start. Keys and hashes never appear here."""
    tls = "on" if cfg.tls_cert and cfg.tls_key else "off"
    return (f"vigil-relay {__version__} on :{cfg.port} (tls {tls}), "
            f"tenants: {','.join(sorted(cfg.tenants))}")


def _ssl_context(cfg: Config):
    if cfg.tls_cert and cfg.tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        return ctx
    return None


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
    cfg = from_env()
    print(banner(cfg), flush=True)
    web.run_app(make_app(cfg), port=cfg.port, ssl_context=_ssl_context(cfg), access_log=None)


if __name__ == "__main__":
    main()
