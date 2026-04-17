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
