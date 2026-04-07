"""Ed25519 task signature verification with TOFU public key pinning.

On first contact the agent stores the server's public key (Trust-On-First-Use).
If the server later presents a different key, all tasks are rejected and an error
is logged. A legitimate key rotation requires the admin to delete the pinned key
file on the agent host — intentional friction for a security-critical operation.
"""

import base64
import json
import logging
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger("vigil.verify")

_PIN_FILENAME = "server_public_key.pin"


class KeyMismatchError(Exception):
    """Raised when the server presents a public key different from the pinned one."""


def _pin_path(data_dir: Path) -> Path:
    return data_dir / _PIN_FILENAME


def pin_public_key(data_dir: Path, key_b64: str) -> VerifyKey:
    """Pin the server's public key using TOFU. Returns the VerifyKey.

    Raises KeyMismatchError if a different key was already pinned.
    """
    pin_file = _pin_path(data_dir)
    key_b64 = key_b64.strip()

    if pin_file.exists():
        stored = pin_file.read_text().strip()
        if stored != key_b64:
            raise KeyMismatchError(
                f"Server public key has changed! Pinned key and received key differ. "
                f"If this is a legitimate key rotation, delete {pin_file} and restart the agent."
            )
        return VerifyKey(base64.b64decode(stored))

    # First contact — pin the key
    data_dir.mkdir(parents=True, exist_ok=True)
    pin_file.write_text(key_b64)
    pin_file.chmod(0o600)
    logger.info("Pinned server public key to %s", pin_file)
    return VerifyKey(base64.b64decode(key_b64))


def get_pinned_key(data_dir: Path) -> VerifyKey | None:
    """Return the pinned VerifyKey, or None if no key is pinned yet."""
    pin_file = _pin_path(data_dir)
    if not pin_file.exists():
        return None
    stored = pin_file.read_text().strip()
    return VerifyKey(base64.b64decode(stored))


def verify_task_signature(task: dict, verify_key: VerifyKey) -> bool:
    """Verify the Ed25519 signature on a task payload.

    The canonical payload must match exactly what the server signs
    (see server/vigil/signing.py:sign_task).
    """
    signature_b64 = task.get("signature", "")
    if not signature_b64:
        logger.warning("Task %s has no signature — rejecting", task.get("id"))
        return False

    # Reconstruct the canonical payload the server signs
    canonical = json.dumps(
        {
            "id": task["id"],
            "host_id": task.get("host_id", ""),
            "action": task["action"],
            "params": task.get("params", {}),
            "nonce": task["nonce"],
            "ttl_seconds": task.get("ttl_seconds", 300),
        },
        sort_keys=True,
    ).encode()

    try:
        signature = base64.b64decode(signature_b64)
        verify_key.verify(canonical, signature)
        return True
    except (BadSignatureError, Exception) as exc:
        logger.warning("Task %s signature verification failed: %s", task.get("id"), exc)
        return False
