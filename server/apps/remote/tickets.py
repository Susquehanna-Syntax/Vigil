"""Session tickets: how the agent learns a session exists.

Nothing between Vigil and the agent — the relay, a proxy, anything — may be able
to tell an agent "open a shell for someone" or point one at another machine. So
the wake message carries a ticket signed with the task signing key and bound to
the *agent's* token fingerprint (the same identity a v2 task signature binds
to). A forged or redirected ticket fails verification on the agent, which is
why ``verify_ticket`` lives here: it is the reference implementation QA-57 ports
into the agent, byte for byte.
"""

import base64
import binascii
import json
import time

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from vigil.signing import agent_fingerprint, get_public_key_b64, get_signing_key

TICKET_TTL_SECONDS = 60
TICKET_VERSION = 1
KINDS = frozenset({"shell", "container", "rdp"})


def make_ticket(session) -> dict:
    """The wake payload for *session*: who it is for, what to run, and a
    signature over all of it. ``exp`` is short because the ticket travels
    through the relay; replaying one after 60 s must be useless."""
    body = {
        "sid": str(session.id),
        "host_id": str(session.host_id),
        "agent": session.host.token_fingerprint,
        "kind": session.kind,
        "container": session.container,
        "exp": int(time.time()) + TICKET_TTL_SECONDS,
        "nonce": session.nonce,
        "v": TICKET_VERSION,
    }
    sig = base64.b64encode(
        get_signing_key().sign(_canonical(body)).signature
    ).decode()
    return {**body, "sig": sig}


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True).encode()


def verify_ticket(ticket, verify_key_b64: str | None = None,
                  agent_token: str = "", now: float | None = None) -> dict:
    """The check the agent performs. Raises ``ValueError`` on anything that
    should make it refuse; returns the ticket body otherwise."""
    if not isinstance(ticket, dict):
        raise TypeError("ticket is not an object")
    sig = ticket.get("sig")
    if not isinstance(sig, str) or not sig:
        raise ValueError("ticket has no signature")
    body = {k: v for k, v in ticket.items() if k != "sig"}
    if body.get("v") != TICKET_VERSION:
        raise ValueError(f"unsupported ticket version {body.get('v')!r}")

    try:
        key = VerifyKey(base64.b64decode(verify_key_b64 or get_public_key_b64()))
        key.verify(_canonical(body), base64.b64decode(sig))
    except BadSignatureError as exc:
        raise ValueError("ticket signature does not verify") from exc
    except binascii.Error as exc:
        raise ValueError("ticket is malformed") from exc

    if body.get("agent") != agent_fingerprint(agent_token):
        raise ValueError("ticket is not for this agent")

    exp = body.get("exp")
    if not isinstance(exp, int) or (now if now is not None else time.time()) >= exp:
        raise ValueError("ticket has expired")

    if body.get("kind") not in KINDS:
        raise ValueError(f"unknown session kind {body.get('kind')!r}")
    return body


def public_key_b64() -> str:
    """What the agent needs to verify tickets — the same key it verifies v2
    task signatures with, so QA-57 needs no new configuration."""
    return get_public_key_b64()
