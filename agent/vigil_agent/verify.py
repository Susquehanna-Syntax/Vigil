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


def pin_public_key(data_dir: Path, key_b64: str, configured: str = "") -> VerifyKey:
    """Pin the server's public key. Returns the VerifyKey.

    With *configured* (the key the installer wrote into agent.yml, SEC-3) that
    key is the only one accepted: a different key from the server raises, and
    the pin file is ignored. Without it, trust on first use: the first key seen
    is pinned, and a different one later raises KeyMismatchError.
    """
    key_b64 = key_b64.strip()
    if configured:
        if key_b64 != configured.strip():
            raise KeyMismatchError(
                "Server public key differs from the one this agent was installed with "
                "(server_public_key in agent.yml). Re-run the installer if the server's "
                "key was rotated on purpose.")
        return VerifyKey(base64.b64decode(configured))
    pin_file = _pin_path(data_dir)

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


def get_pinned_key(data_dir: Path, configured: str = "") -> VerifyKey | None:
    """Return the pinned VerifyKey, or None if no key is pinned yet. The key
    from agent.yml, when there is one, wins over the pin file."""
    if configured:
        return VerifyKey(base64.b64decode(configured))
    pin_file = _pin_path(data_dir)
    if not pin_file.exists():
        return None
    stored = pin_file.read_text().strip()
    return VerifyKey(base64.b64decode(stored))


def agent_fingerprint(agent_token: str) -> str:
    """Must match server/vigil/signing.py:agent_fingerprint."""
    import hashlib
    return hashlib.sha256(agent_token.encode()).hexdigest()


def verify_task_signature(task: dict, verify_key: VerifyKey, agent_token: str | None = None) -> bool:
    """Verify the Ed25519 signature on a task payload.

    The canonical payload must match exactly what the server signs
    (see server/vigil/signing.py:sign_task).
    """
    signature_b64 = task.get("signature", "")
    if not signature_b64:
        logger.warning("Task %s has no signature — rejecting", task.get("id"))
        return False

    fields = {
        "id": task["id"],
        "host_id": task.get("host_id", ""),
        "action": task["action"],
        "params": task.get("params", {}),
        "nonce": task["nonce"],
        "ttl_seconds": task.get("ttl_seconds", 300),
    }
    if agent_token is not None:
        # This agent advertises signed_v2, so only a v2 signature counts: one
        # covering when the task was sent and that it was sent to this agent.
        # Accepting v1 here would let whoever relays a task strip both.
        if task.get("sig_v") != 2 or not isinstance(task.get("dispatched_at"), str):
            logger.warning("Task %s is not v2-signed — rejecting", task.get("id"))
            return False
        fields.update({"dispatched_at": task["dispatched_at"],
                       "agent": agent_fingerprint(agent_token), "v": 2})
    # Reconstruct the canonical payload the server signs
    canonical = json.dumps(fields, sort_keys=True).encode()

    try:
        signature = base64.b64decode(signature_b64)
        verify_key.verify(canonical, signature)
        return True
    except (BadSignatureError, Exception) as exc:
        logger.warning("Task %s signature verification failed: %s", task.get("id"), exc)
        return False
