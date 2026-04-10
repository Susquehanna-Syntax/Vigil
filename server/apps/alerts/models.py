import uuid

from django.db import models

from apps.hosts.models import Host


class NotificationChannel(models.Model):
    """A destination for alert notifications (webhook or email)."""

    class Kind(models.TextChoices):
        WEBHOOK = "webhook", "Webhook"
        EMAIL = "email", "Email"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    config = models.JSONField(
        default=dict,
        help_text='Webhook: {"url": "..."}, Email: {"recipients": ["..."]}',
    )
    on_firing = models.BooleanField(default=True)
    on_resolved = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

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
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-fired_at"]

    def __str__(self):
        return f"[{self.severity}] {self.host.hostname}: {self.message}"
