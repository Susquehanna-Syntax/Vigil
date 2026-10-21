"""Relays: the connection points an agent may reach the dashboard through.

A relay is a separate container (QA-53) that agents talk to instead of the
dashboard, so the dashboard can stay on the LAN while only the relay is public.
The **Built-in** relay stands for "agents talk to the dashboard directly"; it is
a real row so the edition limit counts it and disabling it is a decision with a
place to live (``direct_allowed()``).

The link that joins the dashboard to a remote relay is QA-54; here a relay only
carries its key (encrypted at rest, hashed for constant-time comparison by the
relay) and the connection bookkeeping the UI shows.
"""

import base64
import hashlib
import json
import secrets
import uuid

from django.db import models

from apps.hosts.crypto import encrypt_secret
from vigil.signing import get_signing_key


def generate_key() -> str:
    return "vrk_" + secrets.token_urlsafe(32)


def set_key(relay: "Relay", key: str) -> None:
    """Store ``key`` on ``relay`` — no save; the caller saves."""
    relay.key_encrypted = encrypt_secret(key)
    relay.key_sha256 = hashlib.sha256(key.encode()).hexdigest()


class Relay(models.Model):
    class Kind(models.TextChoices):
        BUILTIN = "builtin", "Built-in"
        REMOTE = "remote", "Remote"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    location = models.CharField(max_length=120, blank=True, default="")
    #: Public base URL agents use, e.g. https://relay.example.com or
    #: https://relay.example.com/t/acme. "" for the built-in relay.
    url = models.CharField(max_length=300, blank=True, default="")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.REMOTE)
    #: Position in the fallback list handed to agents (QA-54b). Low first, ties
    #: break on name. Reordering moves agents between relays the admin already
    #: enabled, so — unlike url or enabled — it is not a TOTP-gated edit.
    priority = models.PositiveSmallIntegerField(default=100)
    enabled = models.BooleanField(default=True)
    key_encrypted = models.BinaryField(blank=True, default=b"")
    key_sha256 = models.CharField(max_length=64, blank=True, default="")
    connected = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # "builtin" sorts before "remote".
        ordering = ("kind", "name")

    def __str__(self):
        return self.name


def builtin() -> Relay | None:
    return Relay.objects.filter(kind=Relay.Kind.BUILTIN).first()


def direct_allowed() -> bool:
    """May agents still connect to the dashboard itself?"""
    row = builtin()
    return True if row is None else row.enabled


def enabled_count() -> int:
    return Relay.objects.filter(enabled=True).count()


def relay_urls() -> list[str]:
    """Fallback base URLs the agent tries, in order, when its server_url is
    unreachable. Enabled remote relays only; the built-in one is the server_url."""
    return [
        relay.url
        for relay in Relay.objects.filter(
            kind=Relay.Kind.REMOTE, enabled=True).order_by("priority", "name").exclude(url="")
    ]


def signed_relay_list(agent_token_fingerprint: str) -> dict:
    """The fallback list, signed for one agent.

    A check-in response travels through a relay, and a relay that could hand
    this list unsigned could point every agent on the deployment at an address
    it controls. So the payload carries the agent's token fingerprint and is
    signed with the task key; the agent verifies both before storing a URL.
    """
    payload = {
        "v": 1,
        "agent": agent_token_fingerprint,
        "relays": relay_urls(),
        "direct": direct_allowed(),
    }
    signature = base64.b64encode(
        get_signing_key().sign(json.dumps(payload, sort_keys=True).encode()).signature
    ).decode()
    return {
        "relays": payload["relays"],
        "direct": payload["direct"],
        "relays_sig": signature,
    }
