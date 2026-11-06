import base64
import hashlib
import binascii
import json
import logging

from django.conf import settings
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

_configured: tuple | None = None   # (current SigningKey, next SigningKey | None)
_epoch_cache: tuple | None = None  # (fetched at, [(public key, effective_at)])

#: How long a process trusts its copy of the key history. A switch is
#: scheduled further ahead than this (SWITCH_DELAY_SECONDS), so every worker
#: has read it before it takes effect and they all change key together.
EPOCH_CACHE_SECONDS = 30
SWITCH_DELAY_SECONDS = 60

#: What the active key signs to introduce the next one. "type" keeps it from
#: being mistaken for anything else the key signs (tasks, relay lists,
#: session tickets): those payloads never carry this type.
ROTATION_TYPE = "key-rotation"

_GENERATE_HINT = (
    "Generate one with: "
    "python3 -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\""
)


def _key_from_seed(seed_b64: str, name: str) -> SigningKey:
    from django.core.exceptions import ImproperlyConfigured

    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImproperlyConfigured(
            f"{name} is not valid base64 ({exc}). "
            f"Expected exactly 44 characters ending in '='. Common cause: a "
            f"YAML/Portainer stack stripped the trailing '=' padding — set the "
            f"value via the Environment variables tab, or wrap it in double "
            f"quotes if you inline it in YAML. " + _GENERATE_HINT
        ) from exc
    if len(seed) != 32:
        raise ImproperlyConfigured(
            f"{name} decoded to {len(seed)} bytes — must be "
            f"exactly 32 (Ed25519 seed size). " + _GENERATE_HINT
        )
    return SigningKey(seed)


def configured_keys() -> tuple:
    """(current, next) from VIGIL_SIGNING_KEY_SEED and
    VIGIL_SIGNING_KEY_NEXT_SEED; next is None when unset. Reads no database,
    so the startup check can call it."""
    global _configured
    if _configured is not None:
        return _configured

    from django.core.exceptions import ImproperlyConfigured

    seed_b64 = (getattr(settings, "VIGIL_SIGNING_KEY_SEED", "") or "").strip()
    if seed_b64:
        current = _key_from_seed(seed_b64, "VIGIL_SIGNING_KEY_SEED")
    elif settings.DEBUG:
        logger.warning(
            "VIGIL_SIGNING_KEY_SEED not set — using ephemeral key. "
            "Task signatures will be invalid after restart."
        )
        current = SigningKey.generate()
    else:
        raise ImproperlyConfigured(
            "VIGIL_SIGNING_KEY_SEED must be set in production. " + _GENERATE_HINT
        )
    next_b64 = (getattr(settings, "VIGIL_SIGNING_KEY_NEXT_SEED", "") or "").strip()
    nxt = _key_from_seed(next_b64, "VIGIL_SIGNING_KEY_NEXT_SEED") if next_b64 else None
    if nxt is not None and bytes(nxt.verify_key) == bytes(current.verify_key):
        nxt = None   # the same seed twice is no rotation
    _configured = (current, nxt)
    return _configured


def public_b64(key: SigningKey) -> str:
    return base64.b64encode(bytes(key.verify_key)).decode()


def reset_cache() -> None:
    """Forget the parsed seeds and the key history (tests; after a switch)."""
    global _configured, _epoch_cache
    _configured = None
    _epoch_cache = None


def _epochs() -> list:
    """[(public key, effective_at)], newest first, cached for a few seconds.
    Empty when the table does not exist yet (migrate has not run)."""
    global _epoch_cache
    import time

    if _epoch_cache is not None and time.monotonic() - _epoch_cache[0] < EPOCH_CACHE_SECONDS:
        return _epoch_cache[1]
    from django.db import DatabaseError

    from apps.hosts.models import SigningKeyEpoch
    try:
        rows = list(SigningKeyEpoch.objects.values_list("public_key", "effective_at")[:50])
    except DatabaseError:
        rows = []
    _epoch_cache = (time.monotonic(), rows)
    return rows


def active_public_key(at=None) -> str | None:
    """The public key that signs now, per the key history; None before any
    switch has been made (the current seed signs)."""
    from django.utils import timezone

    at = at or timezone.now()
    for pub, effective in _epochs():
        if effective <= at:
            return pub
    return None


def get_signing_key() -> SigningKey:
    from django.core.exceptions import ImproperlyConfigured

    current, nxt = configured_keys()
    active = active_public_key()
    if active is None:
        return current
    for key in (current, nxt):
        if key is not None and public_b64(key) == active:
            return key
    raise ImproperlyConfigured(
        f"The active signing key ({active[:12]}...) is neither "
        f"VIGIL_SIGNING_KEY_SEED nor VIGIL_SIGNING_KEY_NEXT_SEED. Put its seed "
        f"back: every agent trusts only that key. `manage.py signing_key status` "
        f"shows the key history.")


def get_public_key_b64() -> str:
    return public_b64(get_signing_key())


def next_signing_key() -> SigningKey | None:
    """The key on offer to agents: VIGIL_SIGNING_KEY_NEXT_SEED's, unless it
    is the active key or ever was. Offering a retired key would hand agents
    back the very key a rotation is meant to be rid of."""
    current, nxt = configured_keys()
    if nxt is None:
        return None
    pub = public_b64(nxt)
    if pub == get_public_key_b64() or any(p == pub for p, _ in _epochs()):
        return None
    return nxt


def rotation_payload(next_pub: str) -> bytes:
    """Must match agent/vigil_agent/verify.py:rotation_payload."""
    return json.dumps({"type": ROTATION_TYPE, "v": 1, "next": next_pub}, sort_keys=True).encode()


def rotation_statement() -> dict | None:
    """{"next", "sig"} for the check-in response while a next key is on
    offer: the active key's signature introducing it. An agent takes the next
    key only when this verifies with the key it already trusts."""
    nxt = next_signing_key()
    if nxt is None:
        return None
    pub = public_b64(nxt)
    sig = get_signing_key().sign(rotation_payload(pub)).signature
    return {"next": pub, "sig": base64.b64encode(sig).decode()}


def switch_to_next(note: str = "", delay_seconds: int = SWITCH_DELAY_SECONDS):
    """Record that the next key signs from ``delay_seconds`` from now. The
    first switch also records the key it replaces, so that key is known as
    retired. Returns the new epoch."""
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone

    from apps.hosts.models import SigningKeyEpoch

    nxt = next_signing_key()
    if nxt is None:
        raise ValueError("No next key on offer: set VIGIL_SIGNING_KEY_NEXT_SEED to a new seed.")
    now = timezone.now()
    with transaction.atomic():
        if not SigningKeyEpoch.objects.exists():
            SigningKeyEpoch.objects.create(
                public_key=get_public_key_b64(), effective_at=now - timedelta(seconds=1),
                note="the key in use before the first switch")
        epoch = SigningKeyEpoch.objects.create(
            public_key=public_b64(nxt), effective_at=now + timedelta(seconds=delay_seconds),
            note=note[:200])
    reset_cache()
    return epoch


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

def agent_fingerprint(agent_token: str) -> str:
    """What a v2 signature binds a task to: the SHA-256 of the agent's token.
    The agent knows its token, not its host id, so this is the identity both
    ends can compute."""
    return hashlib.sha256(agent_token.encode()).hexdigest()


def sign_task_v2(task, dispatched_at: str, agent_fingerprint_hex: str) -> str:
    """Signature over the v1 fields plus when the task was sent and to which
    agent (SEC-4). v1 left the dispatch time unsigned, so the TTL could be
    restarted by whoever relayed the task, and it named the target host
    without the agent ever checking it was the target."""
    payload = json.dumps(
        {
            "id": str(task.id),
            "host_id": str(task.host_id),
            "action": task.action,
            "params": task.params,
            "nonce": task.nonce,
            "ttl_seconds": task.ttl_seconds,
            "dispatched_at": dispatched_at,
            # The SHA-256 of the agent's token, which is exactly how the token
            # is stored (Host.token_fingerprint); the agent hashes its own.
            "agent": agent_fingerprint_hex,
            "v": 2,
        },
        sort_keys=True,
    ).encode()
    return base64.b64encode(get_signing_key().sign(payload).signature).decode()
