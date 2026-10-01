"""Run a policy: dispatch its compiled task to the hosts that have drifted.

Compliant hosts get nothing — not a task that would report "no change", but
no task at all. With a wave ladder the run is an ordinary staged rollout
restricted to the drifted hosts; without one every drifted host gets the task
at once, and those tasks expire when the window closes so nothing starts
after it.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from apps.tasks.models import Task, TaskRun

from .compile import compile_policy
from .drift import policy_drift

logger = logging.getLogger("vigil.policies")


def window_end(policy, started=None):
    return (started or now()) + timedelta(hours=policy.window_hours)


def dispatch_to_hosts(policy, definition, hosts, *, user, started=None) -> TaskRun | None:
    """One signed task per host, grouped under a run, expiring at window end."""
    from apps.tasks.dispatch import resolve_task_spec, task_params

    if not hosts:
        return None
    spec = resolve_task_spec(definition, user=user)
    params, risk, expires_at = task_params(spec)
    closes = window_end(policy, started)
    expires_at = min(expires_at, closes) if expires_at else closes
    with transaction.atomic():
        run = TaskRun.objects.create(
            source=TaskRun.Source.POLICY,
            definition=definition,
            name_snapshot=definition.name,
            requested_by=user,
            host_count=len(hosts),
            step_count=len(params["steps"]),
            state=TaskRun.State.RUNNING,
        )
        for host in hosts:
            Task.objects.create(
                host=host, run=run, requested_by=user, step_order=0,
                step_label=definition.name, action="_script", params=params,
                risk_level=risk, state=Task.State.PENDING, expires_at=expires_at,
                nonce=secrets.token_hex(32),
            )
    return run


def run_policy(policy, *, user=None) -> dict:
    """Compile, find the drifted hosts, and dispatch (or queue for approval).

    Returns ``{"dispatched": n, "mode": ..., "run_id" | "rollout_id"}``.
    """
    from apps.hosts.models import Host

    definition = compile_policy(policy)
    drift = policy_drift(policy)
    host_ids = [row["host_id"] for row in drift["hosts"]]
    result = {"dispatched": 0, "mode": "none", "compliant": drift["compliant"],
              "unknown": len(drift["unknown"])}
    if definition is None or not host_ids:
        return result

    if policy.approval_mode == policy.ApprovalMode.APPROVE:
        from .approval import queue_changes
        result.update(mode="approval", queued=queue_changes(policy, drift["hosts"]))
        return result

    if policy.wave_group_tag:
        from apps.tasks.rollout import start_rollout
        try:
            rollout = start_rollout(definition, user=user,
                                    wave_group_tag=policy.wave_group_tag,
                                    host_ids=host_ids)
        except ValueError as exc:
            logger.warning("policy %s: rollout refused: %s", policy.name, exc)
            result.update(mode="rollout", error=str(exc))
            return result
        result.update(mode="rollout", dispatched=len(host_ids),
                      rollout_id=str(rollout.pk))
        return result

    hosts = list(Host.objects.filter(pk__in=host_ids).order_by("hostname"))
    run = dispatch_to_hosts(policy, definition, hosts, user=user)
    result.update(mode="direct", dispatched=len(hosts),
                  run_id=str(run.pk) if run else None)
    return result
