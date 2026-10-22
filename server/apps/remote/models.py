import uuid

from django.conf import settings
from django.db import models


class RemoteSession(models.Model):
    """One live session: a browser and an agent, piped together by Vigil.

    The row is the whole authority on state — neither socket decides open or
    closed, only the server does, so a compromised agent cannot mark its own
    session closed to stop the audit.
    """

    class Kind(models.TextChoices):
        SHELL = "shell", "Shell"
        CONTAINER = "container", "Container shell"
        RDP = "rdp", "Remote desktop"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        REFUSED = "refused", "Refused"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE,
                             related_name="remote_sessions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="remote_sessions")
    # Denormalised for the audit trail: a user deleted afterwards must still be
    # attributable in every row that mentions them.
    username = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    container = models.CharField(max_length=128, blank=True, default="")
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    #: Per-session randomness, carried in the ticket so no two tickets for the
    #: same session ever look alike.
    nonce = models.CharField(max_length=64)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=120, blank=True, default="")
    #: browser → agent
    bytes_in = models.BigIntegerField(default=0)
    #: agent → browser
    bytes_out = models.BigIntegerField(default=0)
    #: Path under MEDIA_ROOT, Business only (see apps.remote.recording).
    recording = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.kind} {self.host_id} {self.state}"
