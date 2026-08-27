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

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual deploy"
        AUTOMATION = "automation", "Automation"
        BASELINE = "baseline", "Baseline"
        REPROVISION = "reprovision", "Reprovision"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        TaskDefinition, on_delete=models.SET_NULL, null=True, related_name="runs"
    )
    # What kicked this off. Recorded explicitly rather than inferred from the
    # FKs below, because those go null when the automation or baseline is
    # deleted and the history must still say what it was.
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.MANUAL)
    automation = models.ForeignKey(
        "automations.Automation", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
    )
    baseline = models.ForeignKey(
        "baselines.Baseline", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
    )
    # Which staged rollout this run belongs to, if any. Manual deploys and
    # automation/baseline runs leave it null.
    rollout = models.ForeignKey(
        "tasks.PatchRollout", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
    )
    # Which ring the rollout dispatched this run to, if any.
    ring = models.ForeignKey(
        "tasks.PatchRing", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
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
        # Set when the agent evaluated the step's when: predicate to
        # false and elected not to run it. Skipped steps unblock
        # subsequent steps in the same run — they're a terminal state
        # for the step but not a failure.
        SKIPPED = "skipped", "Skipped"

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
    # Soft-delete for the history view. The audit trail is immutable —
    # "deleting" a terminal task hides it from the feed, but the row
    # (who ran what, where, when, with what result) is never destroyed.
    hidden = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["run", "host", "step_order"]),
            models.Index(fields=["state"]),
        ]

    def __str__(self):
        return f"{self.action} → {self.host.hostname} ({self.state})"


class PatchRing(models.Model):
    """One stage of a staged rollout: every host carrying any of its tags.

    Rings are walked in ascending ``order``; a host that matches several
    rings belongs to the earliest one only (see ``ring_host_ids``), so it is
    never patched twice in one rollout. A host matching no ring is not
    patched by a rollout at all — that is deliberate: opting in by tag is
    safer than opting out.
    """

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=("order",), name="uniq_patch_ring_order"),
        ]

    name = models.CharField(max_length=120)
    order = models.PositiveIntegerField()
    tags = models.JSONField(default=list, blank=True)
    # How long the ring must sit after completion before the next one may
    # start. Zero means no soak — the beat advances immediately.
    soak_hours = models.PositiveIntegerField(default=24)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ring:{self.name} ({self.order})"


class PatchRollout(models.Model):
    """One execution of a task definition across the patch rings.

    State machine: ``pending`` → ``running`` (tasks dispatched for the first
    ring) → ``soaking`` (ring passed, waiting out its soak window) → next
    ring ``running`` … → ``completed``. ``halted`` is terminal until an
    operator resumes it; ``cancelled`` is terminal.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SOAKING = "soaking", "Soaking"
        HALTED = "halted", "Halted"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        TaskDefinition, on_delete=models.CASCADE, related_name="rollouts"
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    current_ring = models.ForeignKey(
        PatchRing, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rollouts",
    )
    # Halt when a ring's failure rate is strictly greater than this percent.
    failure_threshold_pct = models.PositiveIntegerField(default=10)
    # Below this many reported results the rate is not evaluated at all —
    # one failure in a one-host canary ring is a 100% failure rate, and
    # without this guard every rollout would halt immediately.
    min_results_before_halt = models.PositiveIntegerField(default=3)
    halted_reason = models.TextField(blank=True)
    # Who halted/resumed, and why/when — the fleet-wide audit for the gate.
    halted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rollouts_halted",
    )
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rollouts_resumed",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ring_started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="rollouts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        ring = self.current_ring.name if self.current_ring else "?"
        return f"rollout:{self.definition_id} @ {ring} ({self.state})"


def ring_host_ids(ring) -> list:
    """Host ids in *ring*: every host carrying any of the ring's tags.

    Mirrors the tag-matching approach used by ``definition_deploy`` (any-tag
    membership, case-insensitive, auto-classified tags included).
    """
    tags = {str(t).lower() for t in (ring.tags or []) if str(t).strip()}
    if not tags:
        return []
    return [
        h.id
        for h in Host.objects.exclude(status=Host.Status.REJECTED)
        if not {str(t).lower() for t in (h.tags or [])}.isdisjoint(tags)
    ]


def rollout_ring_plan(rings) -> dict:
    """Map ring id → host ids, deduplicating across rings in ``order``.

    A host in two rings belongs to the earliest ring only: hosts already
    assigned are skipped so no host is patched twice in one rollout.
    """
    assigned: set = set()
    plan: dict = {}
    for ring in sorted(rings, key=lambda r: r.order):
        hosts = [h for h in ring_host_ids(ring) if h not in assigned]
        assigned.update(hosts)
        plan[ring.id] = hosts
    return plan
