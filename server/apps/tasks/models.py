import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host


class TaskDefinition(models.Model):
    """A user-authored multistep task spec, stored as YAML source-of-truth.

    The parsed spec is cached as JSON so the dispatch path doesn't reparse
    YAML on every deploy. ``visibility`` controls whether the definition is
    private to its owner or browseable as a community template.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        COMMUNITY = "community", "Community"

    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        STANDARD = "standard", "Standard"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="task_definitions",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    relevance = models.CharField(max_length=255, blank=True)
    risk_level = models.CharField(
        max_length=10, choices=RiskLevel.choices, default=RiskLevel.STANDARD
    )
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    yaml_source = models.TextField()
    parsed_spec = models.JSONField(default=dict, blank=True)
    forked_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="forks"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["owner", "-updated_at"]),
            models.Index(fields=["visibility", "-updated_at"]),
        ]

    def __str__(self):
        return self.name

    @property
    def action_count(self) -> int:
        return len(self.parsed_spec.get("actions", []))


class TaskRun(models.Model):
    """A single deploy of a TaskDefinition across one or more hosts.

    Groups the per-host, per-step ``Task`` rows created by one deploy action.
    """

    class State(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        PARTIAL = "partial", "Partial"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        TaskDefinition, on_delete=models.SET_NULL, null=True, related_name="runs"
    )
    name_snapshot = models.CharField(max_length=120, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_runs",
    )
    host_count = models.IntegerField(default=0)
    step_count = models.IntegerField(default=0)
    state = models.CharField(max_length=12, choices=State.choices, default=State.RUNNING)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name_snapshot or 'run'} ({self.state})"


class Task(models.Model):
    """A dispatchable action targeting a specific host agent.

    Tasks may be one-off (no ``run`` / ``definition``) or part of a multistep
    ``TaskRun``. Within a run, only the first step on each host starts
    ``PENDING``; subsequent steps sit in ``BLOCKED`` until the prior step on
    the same host reaches ``COMPLETED``.
    """

    class State(models.TextChoices):
        BLOCKED = "blocked", "Blocked"
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
    run = models.ForeignKey(
        TaskRun, on_delete=models.CASCADE, null=True, blank=True, related_name="tasks"
    )
    step_order = models.IntegerField(default=0)
    step_label = models.CharField(max_length=120, blank=True)

    action = models.CharField(max_length=100)
    params = models.JSONField(default=dict, blank=True)
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.STANDARD)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    nonce = models.CharField(max_length=64, unique=True)
    signature = models.TextField(blank=True)
    ttl_seconds = models.IntegerField(default=300)
    result_output = models.TextField(blank=True)
    # Snapshot of definition.parsed_spec.schedule at deploy time. Used by the
    # checkin dispatcher to gate handoff outside the configured window.
    schedule = models.JSONField(default=dict, blank=True)
    # On-failure retry policy snapshot. retry_count/max_retries track usage.
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=0)
    retry_delay_seconds = models.IntegerField(default=0)
    # When set, the task is held until this time (used between retries).
    not_before = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["run", "host", "step_order"]),
            models.Index(fields=["state"]),
        ]

    def __str__(self):
        return f"{self.action} → {self.host.hostname} ({self.state})"
