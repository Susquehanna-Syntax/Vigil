import base64
import binascii
import json
import logging

from django.conf import settings
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

_signing_key: SigningKey | None = None

_GENERATE_HINT = (
    "Generate one with: "
    "python3 -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\""
)


def get_signing_key() -> SigningKey:
    global _signing_key
    if _signing_key is not None:
        return _signing_key

    from django.core.exceptions import ImproperlyConfigured

    seed_b64 = (getattr(settings, "VIGIL_SIGNING_KEY_SEED", "") or "").strip()
    if seed_b64:
        try:
            seed = base64.b64decode(seed_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImproperlyConfigured(
                f"VIGIL_SIGNING_KEY_SEED is not valid base64 ({exc}). "
                f"Expected exactly 44 characters ending in '='. Common cause: a "
                f"YAML/Portainer stack stripped the trailing '=' padding — set the "
                f"value via the Environment variables tab, or wrap it in double "
                f"quotes if you inline it in YAML. " + _GENERATE_HINT
            ) from exc
        if len(seed) != 32:
            raise ImproperlyConfigured(
                f"VIGIL_SIGNING_KEY_SEED decoded to {len(seed)} bytes — must be "
                f"exactly 32 (Ed25519 seed size). " + _GENERATE_HINT
            )
        _signing_key = SigningKey(seed)
    elif settings.DEBUG:
        logger.warning(
            "VIGIL_SIGNING_KEY_SEED not set — using ephemeral key. "
            "Task signatures will be invalid after restart."
        )
        _signing_key = SigningKey.generate()
    else:
        raise ImproperlyConfigured(
            "VIGIL_SIGNING_KEY_SEED must be set in production. " + _GENERATE_HINT
        )

    return _signing_key


def get_public_key_b64() -> str:
    return base64.b64encode(bytes(get_signing_key().verify_key)).decode()


def sign_task(task) -> str:
    """Return a base64-encoded Ed25519 signature over the canonical task payload."""
    payload = json.dumps(
        {
            "id": str(task.id),
            "host_id": str(task.host_id),
            "action": task.action,
            "params": task.params,
            "nonce": task.nonce,
            "ttl_seconds": task.ttl_seconds,
        },
        sort_keys=True,
    ).encode()
    signed = get_signing_key().sign(payload)
    return base64.b64encode(signed.signature).decode()