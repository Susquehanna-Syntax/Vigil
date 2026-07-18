"""Token verification + key fetch for the Civil SSO callback."""

from __future__ import annotations

import json
import logging
import urllib.request

import jwt
from django.conf import settings

from apps.civilsso.models import CachedCivilKey

logger = logging.getLogger("civilsso")

FETCH_TIMEOUT_SECONDS = 5


def civil_url() -> str:
    return (getattr(settings, "CIVIL_URL", "") or "").rstrip("/")


def app_slug() -> str:
    return getattr(settings, "CIVIL_APP_SLUG", "vigil")


def enabled() -> bool:
    return bool(civil_url())


def get_public_key(*, force_fetch: bool = False) -> str:
    """The cached Civil public key, fetching once on first use.

    The fetch happens at most once per key lifetime — afterwards login
    verification is fully local, so Civil can be down without affecting
    anyone already mapped. An empty return means "can't verify right now":
    the callback fails closed to the normal local login page.
    """
    if not force_fetch:
        cached = CachedCivilKey.current()
        if cached:
            return cached
    url = f"{civil_url()}/api/v1/pubkey/"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            pem = json.loads(resp.read().decode()).get("public_key_pem", "")
    except Exception:  # noqa: BLE001 — any failure = can't verify, not an error page
        logger.exception("could not fetch Civil public key from %s", url)
        return CachedCivilKey.current()
    if pem:
        CachedCivilKey.store(pem, url)
    return pem


def verify_sso_token(token: str) -> dict | None:
    """Verify a Civil SSO token locally. None on any failure — the caller
    redirects to the ordinary login page, never an error page."""
    pem = get_public_key()
    if not pem:
        return None
    try:
        return jwt.decode(
            token, pem, algorithms=["EdDSA"],
            audience=app_slug(), issuer="civil",
        )
    except jwt.PyJWTError as exc:
        logger.warning("Civil SSO token rejected: %s", exc)
        return None
