"""Ed25519 task signature verification with TOFU public key pinning.

On first contact the agent stores the server's public key (Trust-On-First-Use).
If the server later presents a different key, all tasks are rejected and an error
is logged. Rotation (sprint M3 phase 3) needs no visit to the host: while the server
offers a next key, the check-in carries a statement introducing it, signed by
the key this agent trusts. The agent keeps the next key beside the trusted one
only when that signature verifies, acknowledges it, and moves over when the
server starts presenting it. Each move is kept as a signed link in a chain that
starts at the pinned (or configured) key and is re-verified on every load, so a
file planted in data_dir without the old private key proves nothing.
"""

import base64
import json
import logging
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger("vigil.verify")

_PIN_FILENAME = "server_public_key.pin"
_CHAIN_FILENAME = "server_public_key.chain"   # links already moved to, JSON list
_NEXT_FILENAME = "server_public_key.next"     # the next key on offer, JSON object
ROTATION_TYPE = "key-rotation"


class KeyMismatchError(Exception):
    """Raised when the server presents a public key different from the pinned one."""


def _pin_path(data_dir: Path) -> Path:
    return data_dir / _PIN_FILENAME


class KeyRing:
    """The key this agent trusts, plus the next one it has accepted. Duck-types
    nacl's VerifyKey.verify, so every caller (tasks, relay lists, session
    tickets) accepts a signature from either during a rotation."""

    def __init__(self, trusted_b64: str, next_b64: str | None = None):
        self.trusted_b64 = trusted_b64
        self.next_b64 = next_b64
        self._keys = [VerifyKey(base64.b64decode(trusted_b64))]
        if next_b64:
            self._keys.append(VerifyKey(base64.b64decode(next_b64)))

    def __bytes__(self) -> bytes:
        # The trusted key, as VerifyKey gives it: what callers that print or
        # compare "the server's key" mean.
        return base64.b64decode(self.trusted_b64)

    def verify(self, message: bytes, signature: bytes):
        last = None
        for key in self._keys:
            try:
                return key.verify(message, signature)
            except BadSignatureError as exc:
                last = exc
        raise last


def rotation_payload(next_b64: str) -> bytes:
    """Must match server/vigil/signing.py:rotation_payload."""
    return json.dumps({"type": ROTATION_TYPE, "v": 1, "next": next_b64}, sort_keys=True).encode()


def _statement_ok(under_b64: str, statement) -> str | None:
    """The next key a statement introduces, if it is well formed and signed
    by `under_b64`; else None."""
    if not isinstance(statement, dict):
        return None
    nxt, sig = statement.get("next"), statement.get("sig")
    if not (isinstance(nxt, str) and isinstance(sig, str)):
        return None
    try:
        if len(base64.b64decode(nxt, validate=True)) != 32:
            return None
        VerifyKey(base64.b64decode(under_b64)).verify(rotation_payload(nxt),
                                                     base64.b64decode(sig, validate=True))
    except Exception:  # noqa: BLE001 - bad base64, bad key, bad signature: all "no"
        return None
    return nxt


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


def _root(data_dir: Path, configured: str) -> str | None:
    if configured:
        return configured.strip()
    pin_file = _pin_path(data_dir)
    return pin_file.read_text().strip() if pin_file.exists() else None


def _walk_chain(data_dir: Path, root: str) -> tuple[str, list[str]]:
    """(trusted key, every key before it). Each link must be signed by the
    one before; the walk stops at the first that is not."""
    trusted, retired = root, []
    for link in _read_json(data_dir / _CHAIN_FILENAME, []):
        nxt = _statement_ok(trusted, link)
        if nxt is None or nxt == trusted or nxt in retired:
            logger.warning("Ignoring an unverifiable link in %s", data_dir / _CHAIN_FILENAME)
            break
        retired.append(trusted)
        trusted = nxt
    return trusted, retired


def _ring(data_dir: Path, root: str) -> KeyRing:
    trusted, retired = _walk_chain(data_dir, root)
    pending = _statement_ok(trusted, _read_json(data_dir / _NEXT_FILENAME, None))
    if pending in retired or pending == trusted:
        pending = None
    return KeyRing(trusted, pending)


def pin_public_key(data_dir: Path, key_b64: str, configured: str = "") -> KeyRing:
    """Check the key the server presents; return the key ring to verify with.

    With *configured* (the key the installer wrote into agent.yml, SEC-3) that
    key is the root of trust and the pin file is ignored. Without it, trust on
    first use: the first key seen is pinned. Either way the server may present
    the trusted key, or the next key this agent accepted from a signed
    statement (the rotation has happened: move to it). Anything else raises
    KeyMismatchError.
    """
    key_b64 = key_b64.strip()
    root = _root(data_dir, configured)
    if root is None:
        # First contact — pin the key
        _write_private(_pin_path(data_dir), key_b64)
        logger.info("Pinned server public key to %s", _pin_path(data_dir))
        return KeyRing(key_b64)

    ring = _ring(data_dir, root)
    if key_b64 == ring.trusted_b64:
        return ring
    if ring.next_b64 and key_b64 == ring.next_b64:
        statement = _read_json(data_dir / _NEXT_FILENAME, None)
        chain = _read_json(data_dir / _CHAIN_FILENAME, [])
        _write_private(data_dir / _CHAIN_FILENAME, json.dumps(chain + [statement]))
        (data_dir / _NEXT_FILENAME).unlink(missing_ok=True)
        logger.info("The server moved to its next signing key; trusting it from now on")
        return KeyRing(key_b64)
    if configured:
        raise KeyMismatchError(
            "Server public key differs from the one this agent was installed with "
            "(server_public_key in agent.yml) and was not introduced by it. Re-run the "
            "installer if the server's key was replaced without a rotation.")
    raise KeyMismatchError(
        f"Server public key has changed! Pinned key and received key differ, and the "
        f"new one was not introduced by the old. If the server's key was replaced "
        f"without a rotation, delete {_pin_path(data_dir)} and restart the agent."
    )


def learn_rotation(data_dir: Path, statement, configured: str = "") -> str | None:
    """Accept the next key a check-in offers, when the trusted key signed it.
    Returns the accepted next key (what to acknowledge), else None. A key this
    agent trusted before is never taken back."""
    root = _root(data_dir, configured)
    if root is None:
        return None
    trusted, retired = _walk_chain(data_dir, root)
    nxt = _statement_ok(trusted, statement)
    if nxt is None:
        logger.warning("Ignoring a key rotation statement the trusted key did not sign")
        return None
    if nxt == trusted or nxt in retired:
        return None
    current = _read_json(data_dir / _NEXT_FILENAME, None)
    if current != {"next": statement["next"], "sig": statement["sig"]}:
        _write_private(data_dir / _NEXT_FILENAME,
                       json.dumps({"next": statement["next"], "sig": statement["sig"]}))
        logger.info("The server offered its next signing key; holding it for the switch")
    return nxt


def pending_next_key(data_dir: Path, configured: str = "") -> str | None:
    """The accepted next key, to acknowledge at check-in."""
    root = _root(data_dir, configured)
    return _ring(data_dir, root).next_b64 if root else None


def get_pinned_key(data_dir: Path, configured: str = "") -> KeyRing | None:
    """The key ring to verify with, or None if no key is pinned yet. The key
    from agent.yml, when there is one, is the root instead of the pin file."""
    root = _root(data_dir, configured)
    return _ring(data_dir, root) if root else None


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
