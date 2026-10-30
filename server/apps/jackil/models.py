"""The link between a Vigil alert and the Jackil ticket it opened.

This table is the whole reason the integration is not a webhook. A webhook
fires again every time the alert re-fires and leaves the helpdesk holding six
tickets for one bad disk; a stored link means the second event finds the first
ticket and posts to it instead.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models

#: Integration keys are kept as a SHA-256, never in the clear: the rule that
#: governs agent tokens (apps/hosts/models.py) applies with more force here,
#: because a helpdesk key is a standing read grant over the whole inventory
#: and a database dump must not hand one out.
KEY_PREFIX = "vjk_"
#: The stored form's marker, matching ``hosts.models.TOKEN_PREFIX``.
KEY_HASH_PREFIX = "sha256$"


def hash_integration_key(raw: str) -> str:
    """The stored form of a plaintext key. Always hashes, so a caller holding
    a stored value cannot present it as if it were the key."""
    return KEY_HASH_PREFIX + hashlib.sha256(str(raw).encode()).hexdigest()


class IntegrationKey(models.Model):
    """A bearer credential a Jackil deployment presents to read Vigil.

    The row is what a request resolves to and the plaintext exists only in the
    mint response. ``created_by`` is the whole authorization story: a key sees
    exactly what its creator can see and nothing else, so revoking a person
    revokes the helpdesk with them — which is why an inactive creator stops a
    key that was never itself revoked.
    """

    name = models.CharField(max_length=80)
    #: The first 8 characters after "vjk_", shown in lists so an admin can
    #: tell keys apart. Never enough to use.
    prefix = models.CharField(max_length=8)
    key_hash = models.CharField(max_length=80, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE, related_name="jackil_integration_keys")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    #: Off by default: a key is a read grant, and writing is a separate
    #: decision an admin makes per key. Two admins must agree — the creator's
    #: role and this flag — before a helpdesk may change the inventory.
    can_write_devices = models.BooleanField(default=False)

    @classmethod
    def mint(cls, *, name: str, user) -> tuple[IntegrationKey, str]:
        """Create a key; return (row, plaintext). The plaintext exists only here."""
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        row = cls.objects.create(
            name=name, prefix=raw[len(KEY_PREFIX):len(KEY_PREFIX) + 8],
            key_hash=hash_integration_key(raw), created_by=user)
        return row, raw

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __str__(self) -> str:
        return f"{self.name} (vjk_{self.prefix}…)"


class JackilTicket(models.Model):
    """One alert, one ticket.

    ``OneToOne`` is the constraint doing the work: the database refuses a
    second ticket for the same alert even if two workers race the same event.
    """

    alert = models.OneToOneField(
        "alerts.Alert", on_delete=models.CASCADE, related_name="jackil_ticket")
    ticket_id = models.PositiveIntegerField()

    #: The absolute URL as it was when the ticket was opened. Stored rather
    #: than rebuilt, so moving Jackil later does not silently rewrite history
    #: into links that 404.
    url = models.URLField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    #: Set when Vigil posted the "alert cleared" note, whether or not it also
    #: resolved the ticket. Prevents a flapping alert posting the same note
    #: over and over.
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"jackil#{self.ticket_id} for alert {self.alert_id}"


class JackilWorkTicket(models.Model):
    """A ticket a task opened for one host and one key (2026.14.6).

    The key is the author's name for "this problem on this host", so a
    playbook that runs every night finds last night's ticket instead of
    opening another. A closed link stays as history; a new failure after it
    opens a fresh ticket.
    """

    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE,
                             related_name="jackil_work_tickets")
    key = models.CharField(max_length=80)
    ticket_id = models.PositiveIntegerField()
    url = models.URLField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["host", "key"], condition=models.Q(closed_at__isnull=True),
            name="uniq_open_jackil_work_ticket")]

    def __str__(self) -> str:
        return f"jackil#{self.ticket_id} for {self.host_id}/{self.key}"
