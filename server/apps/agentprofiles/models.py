"""Named sets of ``agent.yml`` settings, and the tokens that hand them out.

A profile shapes an agent's config **at install and rebuild only**. After that
the host's own ``agent.yml`` is the authority — nothing here can raise the
privileges of an installed agent. The one-time ``EnrollConfig`` token is the
whole delivery mechanism: it is hashed at rest, expires, and redeems once.
"""
import uuid

from django.conf import settings as django_settings
from django.db import models


class AgentProfile(models.Model):
    """One named set of agent settings."""

    MODES = ("monitor", "managed", "full_control")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=300, blank=True, default="")
    #: Seeded by migration. Editable — an admin who wants "Managed server" to
    #: also pull images should be able to edit it — but never deletable, so the
    #: wizard always has its three starting points.
    builtin = models.BooleanField(default=False)
    mode = models.CharField(max_length=12, default="monitor")
    #: Action names, used in managed mode only.
    allowlist = models.JSONField(default=list, blank=True)
    #: Null means "this dashboard": the agent talks to Vigil directly.
    primary_relay = models.ForeignKey(
        "relays.Relay", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")
    #: Ordered relay ids (str) — an ordered list rather than a FK set because
    #: the order *is* the data and the profile is written whole from the API.
    fallback_relays = models.JSONField(default=list, blank=True)
    #: Add this dashboard as the last fallback when the primary is a relay, so
    #: an agent can still reach Vigil if every relay is down.
    include_dashboard_fallback = models.BooleanField(default=True)
    allow_remote_shell = models.BooleanField(default=False)
    allow_remote_desktop = models.BooleanField(default=False)
    remote_shell_user = models.CharField(max_length=32, default="root")
    remote_desktop_port = models.PositiveIntegerField(default=3389)
    allow_reprovision = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)
    process_watch = models.JSONField(default=list, blank=True)
    checkin_interval = models.PositiveIntegerField(default=60)
    docker_check_interval = models.PositiveIntegerField(default=21600)
    software_interval = models.PositiveIntegerField(default=21600)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Builtins first: the wizard lists its three starting points above the
        # admin's own profiles.
        ordering = ["-builtin", "name"]  # noqa: RUF012

    def __str__(self):
        return self.name


class EnrollConfig(models.Model):
    """A one-time, expiring offer of a rendered ``agent.yml``.

    The installer (QA-63) redeems it at GET /agent/enroll-config/<token>/. Only
    the hash is stored, so a database read never yields a usable token, and the
    redemption is a conditional UPDATE whose rowcount is the lock.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    profile = models.ForeignKey(AgentProfile, null=True, blank=True,
                                on_delete=models.SET_NULL,
                                related_name="enroll_configs")
    #: The validated, fully rendered agent.yml keys. No agent_token: that is
    #: the enrolment step's job, and this row must hold nothing per-host.
    settings = models.JSONField()
    created_by = models.ForeignKey(django_settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]  # noqa: RUF012

    def __str__(self):
        return f"enroll-config {str(self.id)[:8]}"
