import base64
import json
import logging

from django.conf import settings
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

_signing_key: SigningKey | None = None


def get_signing_key() -> SigningKey:
    global _signing_key
    if _signing_key is not None:
        return _signing_key

    seed_b64 = getattr(settings, "VIGIL_SIGNING_KEY_SEED", "")
    if seed_b64:
        _signing_key = SigningKey(base64.b64decode(seed_b64))
    elif settings.DEBUG:
        logger.warning(
            "VIGIL_SIGNING_KEY_SEED not set — using ephemeral key. "
            "Task signatures will be invalid after restart."
        )
        _signing_key = SigningKey.generate()
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("VIGIL_SIGNING_KEY_SEED must be set in production.")

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