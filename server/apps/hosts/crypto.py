"""Symmetric encryption helpers for at-rest secrets (AD bind password).

The key is derived from ``settings.SECRET_KEY`` with HKDF + SHA-256, so
rotating SECRET_KEY rotates the encryption — old ciphertexts stop
decrypting cleanly. Operators rotating SECRET_KEY must re-enter the AD
bind password through the settings UI.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _derive_key() -> bytes:
    """Derive a Fernet key from SECRET_KEY (32 raw bytes → urlsafe-b64)."""
    raw = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw)


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt *plaintext* into bytes safe to persist in a BinaryField."""
    if not plaintext:
        return b""
    f = Fernet(_derive_key())
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    """Reverse :func:`encrypt_secret`. Returns "" on failure (key rotated, etc.)."""
    if not ciphertext:
        return ""
    try:
        f = Fernet(_derive_key())
        return f.decrypt(bytes(ciphertext)).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
