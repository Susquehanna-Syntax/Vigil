"""Automations — run a task or baseline automatically, on an event or a
schedule. Free for everyone (part of the baselines/automation surface).

Two trigger kinds:

- **event**: a lifecycle hook fires (an alert, a host approval, …) and, if the
  optional filters match, the action runs. Alert events target the host that
  raised the alert by default, which is the useful case: "when disk-critical
  fires on a host, run the cleanup baseline on THAT host."
- **schedule**: a crontab drives it via Celery beat. "Every night at 2am, run
  the backup task on the backup hosts."

The action is either a single task definition or a named baseline (expanded
through the baseline machinery, so a scheduled automation can run a whole
sequence). Dispatch reuses the same signed-task path as everything else — the
agent still validates every action against its own allowlist.
"""

import uuid

from django.conf import settings
from django.db import models


class Automation(models.Model):
    class Trigger(models.TextChoices):
        EVENT = "event", "When an event fires"
        SCHEDULE = "schedule", "On a schedule"

    class ActionKind(models.TextChoices):
        TASK = "task", "Task definition"
        BASELINE = "baseline", "Baseline"

    class Target(models.TextChoices):
        EVENT_HOST = "event_host", "The host from the event"
        TAGS = "tags", "Hosts matching tags"
        HOST = "host", "A specific host"
        ALL = "all", "All managed hosts"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    enabled = models.BooleanField(default=True)

    trigger = models.CharField(max_length=12, choices=Trigger.choices)

    # -- event trigger --
    event = models.CharField(max_length=40, blank=True, default="")  # a KNOWN_EVENTS name
    # Only fire when the alert is at least this severity (alert events only).
    min_severity = models.CharField(max_length=10, blank=True, default="")
    # Only fire for a SPECIFIC alert rule, not just any alert (alert events
    # only; null = any rule).
    event_rule = models.ForeignKey(
        "alerts.AlertRule", null=True, blank=True, on_delete=models.CASCADE,
        related_name="automations")
    # Only fire when the event's host carries one of these tags (blank = any).
    event_tags = models.JSONField(default=list, blank=True)

    # -- schedule trigger (crontab; beat-driven) --
    cron_minute = models.CharField(max_length=64, blank=True, default="0")
    cron_hour = models.CharField(max_length=64, blank=True, default="*")
    cron_dom = models.CharField(max_length=64, blank=True, default="*")
    cron_month = models.CharField(max_length=64, blank=True, default="*")
    cron_dow = models.CharField(max_length=64, blank=True, default="*")
    periodic_task = models.ForeignKey(
        "django_celery_beat.PeriodicTask", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")

    # -- action --
    action_kind = models.CharField(max_length=12, choices=ActionKind.choices)
    task_definition = models.ForeignKey(
        "tasks.TaskDefinition", null=True, blank=True,
        on_delete=models.CASCADE, related_name="automations")
    baseline_name = models.CharField(max_length=120, blank=True, default="")
    # Input overrides for the task action: {"<action_index>": {"<param>": value}}
    # merged over the definition's params at dispatch (TASK kind only).
    params_override = models.JSONField(default=dict, blank=True)

    # -- target (ignored for EVENT_HOST) --
    target = models.CharField(max_length=12, choices=Target.choices,
                              default=Target.EVENT_HOST)
    target_tags = models.JSONField(default=list, blank=True)
    target_host = models.ForeignKey(
        "hosts.Host", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="automations")
    last_run = models.DateTimeField(null=True, blank=True)
    run_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"automation:{self.name}"

    @property
    def cron_display(self) -> str:
        return " ".join([self.cron_minute, self.cron_hour, self.cron_dom,
                         self.cron_month, self.cron_dow])
