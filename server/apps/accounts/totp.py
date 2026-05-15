"""RFC 6238 TOTP, implemented inline to avoid a pyotp dependency.

Secrets are base32-encoded (RFC 3548). We use the SHA-1 variant with a
6-digit code and a 30-second step, matching every authenticator app in
common use (Google Authenticator, 1Password, Authy, Bitwarden, Aegis…).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote


_STEP_SECONDS = 30
_DIGITS = 6


def generate_secret(length_bytes: int = 20) -> str:
    """Return a fresh base32-encoded secret (default: 160 bits, per RFC 4226)."""
    raw = os.urandom(length_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    # Re-pad to a multiple of 8 so base32decode accepts it.
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(code_int % (10 ** _DIGITS)).zfill(_DIGITS)


def generate_totp(secret_b32: str, at: float | None = None) -> str:
    counter = int((at if at is not None else time.time()) // _STEP_SECONDS)
    return _hotp(secret_b32, counter)


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    """Constant-time compare of ``code`` against the current TOTP, ±window steps."""
    if not secret_b32 or not code:
        return False
    code = code.strip().replace(" ", "")
    if len(code) != _DIGITS or not code.isdigit():
        return False
    now = int(time.time() // _STEP_SECONDS)
    for drift in range(-window, window + 1):
        candidate = _hotp(secret_b32, now + drift)
        if hmac.compare_digest(candidate, code):
            return True
    return False


def otpauth_uri(secret_b32: str, account_name: str, issuer: str = "Vigil") -> str:
    """Return the ``otpauth://`` URI you can stuff into a QR code."""
    label = quote(f"{issuer}:{account_name}")
    params = f"secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={_DIGITS}&period={_STEP_SECONDS}"
    return f"otpauth://totp/{label}?{params}"


# A code is valid for up to (1 step + window) before and after the current
# step. With window=1 and step=30s that's a 90-second total validity window,
# which is also how long we treat a freshly-consumed code as "burned".
_REPLAY_WINDOW_SECONDS = (2 * 1 + 1) * _STEP_SECONDS


def consume_totp(user, code: str) -> tuple[bool, str | None]:
    """Verify a TOTP code for ``user`` and mark it consumed.

    Rejects codes that were used within the validity window so an
    intercepted code cannot be replayed for a second sensitive action.
    Returns ``(ok, error_message)``.
    """
    from django.utils.timezone import now

    profile = getattr(user, "profile", None)
    if profile is None:
        return False, "User profile missing"
    secret = profile.totp_secret or ""
    if not (profile.totp_confirmed_at and secret):
        return False, "TOTP enrollment required — enroll in Settings before continuing"

    code = (code or "").strip().replace(" ", "")
    if not code:
        return False, "TOTP code required"
    if not verify_totp(secret, code):
        return False, "Invalid TOTP code"

    if profile.last_totp_code == code and profile.last_totp_used_at:
        elapsed = (now() - profile.last_totp_used_at).total_seconds()
        if elapsed < _REPLAY_WINDOW_SECONDS:
            return False, "TOTP code already used — wait for the next code"

    profile.last_totp_code = code
    profile.last_totp_used_at = now()
    profile.save(update_fields=["last_totp_code", "last_totp_used_at"])
    return True, None


def require_totp_confirmation(user, payload) -> str | None:
    """Standard 2FA gate for sensitive endpoints.

    Pulls ``totp`` from the request payload, verifies it, and marks it
    consumed. Returns an error string for the caller to surface, or
    ``None`` on success.

    Notes:
      * There is NO DEBUG bypass — local development must enroll a real
        TOTP secret. See ``apps/accounts/totp.py:generate_totp`` for a
        helper that prints the current code during testing.
      * Replay protection: the same code cannot be reused within its
        validity window even if it would otherwise verify.
    """
    payload = payload or {}
    ok, err = consume_totp(user, (payload.get("totp") or "").strip())
    return None if ok else err
