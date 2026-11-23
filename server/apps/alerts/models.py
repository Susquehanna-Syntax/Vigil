import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host
from vigil.scoping import SiteScopedManager


class NotificationChannel(models.Model):
    """A destination for alert notifications.

    ``kind`` names a type in apps.alerts.channels, which says what ``config``
    holds; secret settings are stored encrypted (``enc:`` prefix) and never
    returned by the API.
    """

    class Kind(models.TextChoices):
        """The built-in kinds, for code and tests. The registry in
        apps.alerts.channels is the full list: kind is not a choices field, so
        a new channel type needs no migration."""

        WEBHOOK = "webhook", "Webhook"
        EMAIL = "email", "Email"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=32)
    config = models.JSONField(default=dict)
    on_firing = models.BooleanField(default=True)
    on_resolved = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    #: May this channel reach private (LAN) addresses? Set when an admin of
    #: the whole install saves its address, cleared when anyone else does.
    allow_private_targets = models.BooleanField(default=False)
    #: The last delivery, test sends included: None until the first.
    last_ok = models.BooleanField(null=True, blank=True)
    last_status = models.CharField(max_length=200, blank=True, default="")
    last_status_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.kind})"


class AlertRule(models.Model):
    """Defines a condition that triggers an alert."""

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50)  # disk, memory, cpu, docker, etc.
    metric = models.CharField(max_length=100)
    operator = models.CharField(max_length=10)  # gt, lt, gte, lte, eq
    threshold = models.FloatField()
    severity = models.CharField(max_length=10, choices=Severity.choices)
    duration_seconds = models.IntegerField(default=0)  # sustained for N seconds
    enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=True)  # shipped with Vigil vs user-created

    def __str__(self):
        return f"{self.name} ({self.severity})"


class Alert(models.Model):
    """An instance of a fired alert."""

    objects = SiteScopedManager(path="host__", app="alerts")

    class State(models.TextChoices):
        FIRING = "firing", "Firing"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="alerts")
    rule = models.ForeignKey(AlertRule, on_delete=models.SET_NULL, null=True, related_name="alerts")
    state = models.CharField(max_length=20, choices=State.choices, default=State.FIRING)
    severity = models.CharField(max_length=10, choices=AlertRule.Severity.choices)
    message = models.TextField()
    metric_value = models.FloatField(null=True)
    fired_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    # Timed acknowledgement: when set, the ack lapses at this instant and the
    # alert flips back to FIRING (and re-notifies). Null = acknowledged for
    # good — stays quiet until it resolves or is manually un-acknowledged.
    acknowledged_until = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    fix_context = models.JSONField(blank=True, default=dict)

    #: Set by a `raise_alert` task action: the author's name for "this problem
    #: on this host", so a repeat run deduplicates against it and a
    #: `resolve_alert` step finds the alert again. Empty for every alert the
    #: metric evaluator opens.
    source_key = models.CharField(max_length=80, blank=True, default="", db_index=True)

    #: How many times this alert has re-fired inside the flap window without
    #: paging again. Zero is the ordinary case; a high number is the signal
    #: that a threshold is set where a metric lives.
    flap_count = models.PositiveIntegerField(default=0)

    #: When the firing notice went out to the channels. Null when nothing was
    #: sent (the host was in maintenance, or the alert predates this field).
    notified_at = models.DateTimeField(null=True, blank=True)

    #: The alert resolved somewhere that must not send (an agent's check-in) and
    #: its resolved notice is still owed; the stale-host job sends it.
    resolve_pending = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-fired_at"]

    def __str__(self):
        return f"[{self.severity}] {self.host.hostname}: {self.message}"


class MaintenanceWindow(models.Model):
    """A time when some hosts are expected to be down or noisy: no metric
    alerts fire for them and no notifications go out (apps/alerts/maintenance.py)."""

    class Kind(models.TextChoices):
        ONCE = "once", "One-off"
        WEEKLY = "weekly", "Weekly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    #: Null = global (every site's hosts, unless hosts or tags narrow it).
    site_id = models.UUIDField(null=True, blank=True, db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ONCE)
    # one-off
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    # weekly: Monday = 0 ... Sunday = 6, as datetime.weekday()
    weekdays = models.JSONField(default=list, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=60)
    timezone = models.CharField(max_length=64, default="UTC")
    #: Empty hosts and tags = every host in the window's site.
    hosts = models.ManyToManyField(Host, blank=True, related_name="maintenance_windows")
    tags = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class DeviceAlert(models.Model):
    """A device check failed twice in a row (S9). Separate from Alert, which is host-bound."""

    # The manager's own scope narrowing does the site work: the same `field=`
    # form DeviceCheck uses, following the device's own site_id column.
    objects = SiteScopedManager(field="device__site_id", app="alerts")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey("hosts.UnmanagedDevice", on_delete=models.CASCADE,
                               related_name="alerts")
    device_check = models.ForeignKey("hosts.DeviceCheck", on_delete=models.SET_NULL,
                                     null=True, related_name="alerts")
    state = models.CharField(max_length=20, choices=Alert.State.choices,
                             default=Alert.State.FIRING)
    #: The same three states as a host alert: the API and the page speak one vocabulary.
    State = Alert.State
    severity = models.CharField(max_length=10, default="critical")
    message = models.TextField()
    fired_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                        null=True, blank=True, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-fired_at"]
        indexes = [
            models.Index(fields=["device", "state"], name="devicealert_device_state"),
            models.Index(fields=["state", "resolved_at"], name="devicealert_state_resolved"),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.device_id}: {self.message}"
