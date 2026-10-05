"""Celery entry point for policy windows, and keeping beat in step."""

import json
import logging

from celery import shared_task

logger = logging.getLogger("vigil.policies")


@shared_task
def run_scheduled_policy(policy_id):
    """Beat fires this when a policy's window opens."""
    from .models import UpdatePolicy
    from .run import run_policy

    policy = UpdatePolicy.objects.filter(pk=policy_id, enabled=True).first()
    if policy is None:
        return "skipped"
    return f"dispatched:{run_policy(policy, user=policy.created_by)['dispatched']}"


def task_name(policy) -> str:
    return f"policy:{policy.id}"


def sync_periodic_task(policy) -> None:
    """One beat task per enabled policy, at its window's start. A disabled
    policy has none — the row is deleted rather than left disabled, so a
    re-enable starts from the policy's current window."""
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
    except Exception:  # noqa: BLE001 — beat not installed: windows just won't open
        logger.warning("django_celery_beat unavailable; policy window not synced")
        return
    if not policy.enabled or policy.pk is None:
        PeriodicTask.objects.filter(name=task_name(policy)).delete()
        return
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=policy.cron_minute, hour=policy.cron_hour, day_of_month="*",
        month_of_year="*", day_of_week=policy.cron_dow)
    PeriodicTask.objects.update_or_create(
        name=task_name(policy),
        defaults=dict(crontab=schedule, interval=None,
                      task="apps.policies.tasks.run_scheduled_policy",
                      args=json.dumps([str(policy.id)]), enabled=True))


def delete_periodic_task(policy) -> None:
    try:
        from django_celery_beat.models import PeriodicTask
    except Exception:  # noqa: BLE001
        return
    PeriodicTask.objects.filter(name=task_name(policy)).delete()
