import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host


class Task(models.Model):
    """A dispatchable action targeting a specific host agent."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        DISPATCHED = "dispatched", "Dispatched"
        EXECUTING = "executing", "Executing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        STANDARD = "standard", "Standard"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="tasks")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="requested_tasks"
    )
    action = models.CharField(max_length=100)  # restart_service, clear_temp_files, etc.
    params = models.JSONField(default=dict, blank=True)
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.STANDARD)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    nonce = models.CharField(max_length=64, unique=True)
    signature = models.TextField(blank=True)  # Ed25519 signature
    ttl_seconds = models.IntegerField(default=300)
    result_output = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} → {self.host.hostname} ({self.state})"
