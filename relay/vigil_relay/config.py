"""Relay configuration from the environment."""

import json
import os
import re
from dataclasses import dataclass

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_TRUE = ("1", "true", "yes")
_DEFAULT_QUEUE_DIR = "/var/lib/vigil-relay/queue"


@dataclass(frozen=True)
class Config:
    tenants: dict[str, str]          # slug -> sha256 hex of that tenant's relay key
    port: int = 8443
    queue_dir: str = _DEFAULT_QUEUE_DIR
    tls_cert: str = ""
    tls_key: str = ""
    trust_proxy: bool = False
    max_body: int = 10 * 1024 * 1024
    request_timeout: float = 60.0
    queue_max_bytes: int = 50 * 1024 * 1024
    queue_max_items: int = 10_000


def from_env(env=os.environ) -> Config:
    tenants = _tenants(env)

    queue_dir = env.get("RELAY_QUEUE_DIR", "").strip() or _DEFAULT_QUEUE_DIR
    return Config(
        tenants=tenants,
        port=_int(env, "RELAY_PORT", 8443),
        queue_dir=queue_dir,
        tls_cert=env.get("RELAY_TLS_CERT", "").strip(),
        tls_key=env.get("RELAY_TLS_KEY", "").strip(),
        trust_proxy=env.get("RELAY_TRUST_PROXY", "").strip().lower() in _TRUE,
    )


def _tenants(env) -> dict[str, str]:
    raw = env.get("RELAY_TENANTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise SystemExit("RELAY_TENANTS must be a JSON object of slug -> sha256 hex")
        if not isinstance(parsed, dict) or not parsed:
            raise SystemExit("RELAY_TENANTS must be a non-empty JSON object of slug -> sha256 hex")
    else:
        single = env.get("RELAY_KEY_SHA256", "").strip()
        if not single:
            raise SystemExit(
                "set RELAY_TENANTS (a JSON object of slug -> sha256 hex) "
                "or RELAY_KEY_SHA256 (one relay key hash)"
            )
        parsed = {"default": single}

    tenants: dict[str, str] = {}
    for slug, digest in parsed.items():
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise SystemExit(f"RELAY_TENANTS: invalid tenant slug {slug!r}")
        if not isinstance(digest, str) or not HASH_RE.match(digest.strip()):
            raise SystemExit(
                f"RELAY_TENANTS: tenant {slug!r} has an invalid key hash (want 64 lowercase hex chars)"
            )
        tenants[slug] = digest.strip()
    return tenants


def _int(env, name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")
