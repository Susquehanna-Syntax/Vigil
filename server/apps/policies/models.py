"""App and patch policies — the desired state of a group of hosts.

A policy names the hosts it covers (by tag and/or site), says what each app
should look like on them, and — on its Patching tab — which OS updates install
and when. It never acts on its own: it compiles to an ordinary task definition
(phase 04) so signing, approval, audit, waves and windows all apply unchanged.
"""

import uuid

from django.conf import settings
from django.db import models

#: Windows Update classifications a policy may install. The names are the
#: ones the Windows Update Agent reports, so the agent compares them as-is.
WINDOWS_CLASSIFICATIONS = (
    "Critical Updates", "Security Updates", "Definition Updates",
    "Update Rollups", "Updates", "Feature Packs", "Service Packs",
    "Drivers", "Tools", "Upgrades",
)


class UpdatePolicy(models.Model):
    class ApprovalMode(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic"
        APPROVE = "approve", "Approve each change"

    class Reboot(models.TextChoices):
        NEVER = "never", "Never"
        IN_WINDOW = "in_window", "In the window"
        ASK = "ask", "Ask the user"

    class LinuxUpdates(models.TextChoices):
        SECURITY = "security", "Security updates only"
        ALL = "all", "All updates"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    enabled = models.BooleanField(default=True)

    #: Empty = every managed host.
    target_tags = models.JSONField(default=list, blank=True)
    #: business_sites.Site id as a plain UUID — Free never joins a Business
    #: table (see apps/statuspage/models.py). Null = no site restriction.
    site_id = models.UUIDField(null=True, blank=True)

    # The maintenance window: when it opens (cron) and how long it stays open.
    cron_minute = models.CharField(max_length=64, default="0")
    cron_hour = models.CharField(max_length=64, default="2")
    cron_dow = models.CharField(max_length=64, default="*")
    window_hours = models.PositiveSmallIntegerField(default=4)

    #: Patch-wave ladder to roll out on. Blank = every drifted host at once.
    wave_group_tag = models.CharField(max_length=120, blank=True, default="")
    approval_mode = models.CharField(
        max_length=10, choices=ApprovalMode.choices, default=ApprovalMode.AUTOMATIC)

    # -- Patching tab --
    patch_enabled = models.BooleanField(default=False)
    windows_classifications = models.JSONField(default=list, blank=True)
    deferral_days = models.PositiveSmallIntegerField(default=7)
    reboot = models.CharField(max_length=10, choices=Reboot.choices,
                              default=Reboot.IN_WINDOW)
    linux_updates = models.CharField(max_length=10, choices=LinuxUpdates.choices,
                                     default=LinuxUpdates.SECURITY)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="update_policies")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return f"policy:{self.name}"


class AppRule(models.Model):
    class State(models.TextChoices):
        PRESENT = "present", "Present"
        LATEST = "latest", "Latest"
        PINNED = "pinned", "Pinned"
        ABSENT = "absent", "Absent"

    policy = models.ForeignKey(UpdatePolicy, on_delete=models.CASCADE,
                               related_name="app_rules")
    #: Inventory id (SoftwareItem.package_id).
    app = models.CharField(max_length=200)
    #: Blank = the host's own package manager.
    source = models.CharField(max_length=20, blank=True, default="")
    state = models.CharField(max_length=10, choices=State.choices)
    #: Pinned only.
    version = models.CharField(max_length=80, blank=True, default="")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("order", "id")
        constraints = [
            models.UniqueConstraint(fields=("policy", "app", "source"),
                                    name="uniq_policy_app_rule"),
        ]

    def __str__(self) -> str:
        return f"{self.app} {self.state}"
