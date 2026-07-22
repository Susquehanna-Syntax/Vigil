"""Celery entry point for scheduled automations + beat synchronization."""

import json
import logging

from celery import shared_task

logger = logging.getLogger("vigil.automations")


@shared_task
def run_scheduled_automation(automation_id):
    """Beat fires this per scheduled Automation via its PeriodicTask."""
    from .engine import run_automation
    from .models import Automation

    auto = Automation.objects.filter(pk=automation_id, enabled=True,
                                     trigger=Automation.Trigger.SCHEDULE).first()
    if auto is None:
        return "skipped"
    n = run_automation(auto)
    return f"dispatched:{n}"


def sync_periodic_task(automation) -> None:
    """Create/update the django_celery_beat PeriodicTask for a scheduled
    automation so beat runs it. Disabled or non-schedule automations get their
    task disabled. Safe to call on every save."""
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
    except Exception:  # noqa: BLE001 — beat not installed: schedules just won't run
        logger.warning("django_celery_beat unavailable; schedule not synced")
        return

    if automation.trigger != automation.Trigger.SCHEDULE:
        _disable_task(automation)
        return

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=automation.cron_minute or "0",
        hour=automation.cron_hour or "*",
        day_of_month=automation.cron_dom or "*",
        month_of_year=automation.cron_month or "*",
        day_of_week=automation.cron_dow or "*",
    )
    name = f"automation:{automation.id}"
    pt, _ = PeriodicTask.objects.update_or_create(
        name=name,
        defaults=dict(
            crontab=schedule,
            interval=None,
            task="apps.automations.tasks.run_scheduled_automation",
            args=json.dumps([str(automation.id)]),
            enabled=automation.enabled,
        ),
    )
    if automation.periodic_task_id != pt.id:
        automation.periodic_task = pt
        automation.save(update_fields=["periodic_task"])


def _disable_task(automation):
    if automation.periodic_task_id:
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(pk=automation.periodic_task_id).update(enabled=False)
