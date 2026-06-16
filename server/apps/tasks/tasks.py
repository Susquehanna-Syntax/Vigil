"""Periodic maintenance for the task pipeline.

``expire_stale_tasks`` is the safety net for tasks that left the server
but never came back: the agent crashed mid-execution, dropped offline
after pickup, or hit an error path that failed to report. Without this
sweep those rows sit in ``DISPATCHED`` forever — they can't be deleted
from the history view (in-flight states are protected) and their runs
never finalize.

A task is considered stale once ``dispatched_at`` is older than its own
``ttl_seconds`` plus a grace period. The TTL bounds when an agent may
*start* the task; the grace period covers legitimately long executions
(e.g. a filesystem-wide Trivy scan) plus one full check-in cycle for
the result POST to land.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from .models import Task

logger = logging.getLogger(__name__)


@shared_task(name="tasks.expire_stale_tasks")
def expire_stale_tasks() -> str:
    """Flip overdue DISPATCHED/EXECUTING tasks to EXPIRED and finalize runs."""
    from .views import _finalize_run_if_done

    grace = int(getattr(settings, "VIGIL_TASK_EXPIRY_GRACE_SECONDS", 3600))
    current = now()

    # Candidate set first (cheap, indexed on state); the per-task TTL
    # check happens in Python because ttl_seconds varies per row and
    # datetime arithmetic on a column isn't portable to SQLite.
    candidates = Task.objects.filter(
        state__in=[Task.State.DISPATCHED, Task.State.EXECUTING],
        dispatched_at__isnull=False,
        dispatched_at__lt=current - timedelta(seconds=grace),
    ).select_related("run")

    expired = 0
    runs = {}
    for task in candidates:
        deadline = task.dispatched_at + timedelta(seconds=task.ttl_seconds + grace)
        if current < deadline:
            continue

        task.state = Task.State.EXPIRED
        task.completed_at = current
        note = (
            f"[expired by server: no result {task.ttl_seconds + grace}s "
            f"after dispatch]"
        )
        prior = (task.result_output or "").rstrip()
        task.result_output = f"{prior}\n{note}".strip()
        task.save(update_fields=["state", "completed_at", "result_output"])
        expired += 1
        logger.warning(
            "Task %s on host %s expired — dispatched %s, never reported",
            task.id, task.host_id, task.dispatched_at,
        )

        if task.run_id:
            runs[task.run_id] = task.run
            # Any steps still BLOCKED behind this one will never unblock.
            Task.objects.filter(
                run=task.run, host=task.host, state=Task.State.BLOCKED,
                step_order__gt=task.step_order,
            ).update(
                state=Task.State.REJECTED,
                result_output=f"Aborted: step {task.step_order} expired",
                completed_at=current,
            )

    for run in runs.values():
        _finalize_run_if_done(run)

    if not expired:
        return "no stale tasks"
    return f"expired {expired} task(s) across {len(runs)} run(s)"
