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
from apps.instance.config import setting
from django.utils.timezone import now

from .models import PatchRollout, Task

logger = logging.getLogger(__name__)


@shared_task(name="tasks.expire_stale_tasks")
def expire_stale_tasks() -> str:
    """Flip overdue DISPATCHED/EXECUTING tasks to EXPIRED and finalize runs."""
    from .views import _finalize_run_if_done

    grace = int(setting("VIGIL_TASK_EXPIRY_GRACE_SECONDS"))
    current = now()

    # Pending tasks past their expires_at (hunts whose stays_open has elapsed,
    # phase 06b): the host never checked in, so stop waiting and mark it.
    pending_expired = 0
    pending_runs = {}
    for task in Task.objects.filter(
        state=Task.State.PENDING, expires_at__isnull=False, expires_at__lte=current,
    ).select_related("run"):
        task.state = Task.State.EXPIRED
        task.completed_at = current
        task.result_output = (
            f"[did not report: the hunt stayed open until "
            f"{task.expires_at.isoformat()} and the host never checked in]"
        )
        task.save(update_fields=["state", "completed_at", "result_output"])
        pending_expired += 1
        logger.warning(
            "Task %s on host %s did not report — hunt stayed open until %s",
            task.id, task.host_id, task.expires_at,
        )
        if task.run_id:
            pending_runs[task.run_id] = task.run

    for run in pending_runs.values():
        _finalize_run_if_done(run)

    if pending_expired:
        logger.info("Expired %d pending hunt task(s)", pending_expired)

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

    if not expired and not pending_expired:
        return "no stale tasks"
    summary = f"expired {expired} task(s) across {len(runs)} run(s)"
    if pending_expired:
        summary += f"; {pending_expired} hunt task(s) did not report"
    return summary


@shared_task(name="tasks.advance_rollouts")
def advance_rollouts() -> str:
    """Evaluate every active rollout once per beat tick.

    ``evaluate_rollout`` takes the rollout row lock itself, so this sweep is
    safe to run concurrently with itself: two overlapping ticks cannot both
    advance the same rollout and double-dispatch a wave.
    """
    from .rollout import evaluate_rollout

    rollouts = list(
        PatchRollout.objects.filter(
            state__in=[
                PatchRollout.State.RUNNING,
                PatchRollout.State.VALIDATING,
            ]
        ).select_related("definition", "current_wave")
    )
    advanced = 0
    for rollout in rollouts:
        before = rollout.state
        evaluate_rollout(rollout)
        # evaluate_rollout works on a locked copy — refresh to see the result.
        rollout.refresh_from_db()
        if rollout.state != before:
            advanced += 1
            logger.info(
                "Rollout %s advanced %s -> %s",
                rollout.id, before, rollout.state,
            )
    if not rollouts:
        return "no active rollouts"
    return f"evaluated {len(rollouts)} rollout(s), {advanced} advanced"
