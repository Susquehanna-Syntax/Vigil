"""Staged rollout engine: the advance/halt brain for ``PatchRollout``.

Pure-ish over database state so it can be called from the beat task, from a
task-result webhook, and from tests. The one clock read goes through
``_now()`` so tests can patch the soak window without sleeping.
"""

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from .models import (
    PatchRing,
    PatchRollout,
    Task,
    TaskRun,
    rollout_ring_plan,
)
from .spec import SpecError, resolve_inputs

logger = logging.getLogger(__name__)

# Terminal failure states: the task ran (or was given up on) and did not
# succeed. SKIPPED and COMPLETED are non-failure terminals.
FAILURE_STATES = (
    Task.State.FAILED,
    Task.State.EXPIRED,
    Task.State.REJECTED,
)
TERMINAL_STATES = (
    Task.State.COMPLETED,
    Task.State.SKIPPED,
) + FAILURE_STATES


def _now():
    return now()


def _first_enabled_ring():
    return PatchRing.objects.filter(enabled=True).order_by("order", "id").first()


def _next_enabled_ring(after_order):
    return (
        PatchRing.objects.filter(enabled=True, order__gt=after_order)
        .order_by("order", "id")
        .first()
    )


def _validate_definition(definition) -> dict:
    """Prove the definition can be dispatched without operator inputs.

    A rollout runs unattended across rings; a definition with required
    inputs has no value to fill them with, so it is refused at start rather
    than discovered mid-rollout. Returns the resolved spec.
    """
    spec = definition.parsed_spec or {}
    try:
        return resolve_inputs(spec, {})
    except SpecError as exc:
        raise ValueError(f"definition {definition.name!r} cannot roll out: {exc}") from exc


def start_rollout(
    definition,
    user=None,
    failure_threshold_pct: int = 10,
    min_results_before_halt: int = 3,
) -> PatchRollout:
    """Create a rollout and dispatch its first ring.

    Raises ``ValueError`` when no enabled ring exists, the definition
    cannot be dispatched without operator inputs, or the gate settings are
    out of range.
    """
    if not 0 <= failure_threshold_pct <= 100:
        raise ValueError("failure_threshold_pct must be between 0 and 100")
    if min_results_before_halt < 1:
        raise ValueError("min_results_before_halt must be at least 1")
    if _first_enabled_ring() is None:
        raise ValueError("no enabled patch rings")
    spec = _validate_definition(definition)

    ts = _now()
    rollout = PatchRollout.objects.create(
        definition=definition,
        state=PatchRollout.State.RUNNING,
        current_ring=_first_enabled_ring(),
        failure_threshold_pct=failure_threshold_pct,
        min_results_before_halt=min_results_before_halt,
        started_at=ts,
        ring_started_at=ts,
        created_by=user,
    )
    with transaction.atomic():
        _dispatch_ring(rollout, spec)
    return rollout


def _dispatch_ring(rollout: PatchRollout, spec: dict) -> int:
    """Dispatch one task per ring host and open a TaskRun for them.

    Hosts come from the cross-ring plan, not the raw ring membership: a
    host tagged into two rings belongs to the earliest ring only, so this
    ring must not re-claim it. Must run inside a transaction that holds
    the rollout row lock (see ``evaluate_rollout``) — otherwise two beat
    ticks can both pass the "no run yet for this ring" check and dispatch
    the same ring twice. An empty ring still opens a run (zero hosts):
    the gate then sees 0/0, passes, soaks, and advances.
    Returns the number of tasks created.
    """
    from apps.baselines.expansion import _max_risk, expand_actions
    from apps.hosts.models import Host

    enabled = list(
        PatchRing.objects.filter(enabled=True).order_by("order", "id")
    )
    plan = rollout_ring_plan(enabled)
    host_ids = plan.get(rollout.current_ring.id, [])
    hosts = list(Host.objects.filter(id__in=host_ids))

    actions = spec.get("actions") or []
    actions, expanded_risk = expand_actions(actions)
    if not actions:
        raise ValueError("definition has no actions")

    steps_payload = []
    for i, action in enumerate(actions):
        step = {
            "id": action.get("id") or f"step{i + 1}",
            "action": action["type"],
            "params": action.get("params") or {},
        }
        when_expr = action.get("when") or ""
        if when_expr:
            step["when"] = when_expr
        if action.get("timeout"):
            step["timeout"] = action["timeout"]
        steps_payload.append(step)

    risk = _max_risk(spec.get("risk", "standard"), expanded_risk)
    schedule_snapshot = spec.get("schedule") or {}
    retry_cfg = (spec.get("on_failure") or {}).get("retry") or {}
    max_retries = int(retry_cfg.get("attempts", 0))
    retry_delay = int(retry_cfg.get("delay_seconds", 0))

    run = TaskRun.objects.create(
        definition=rollout.definition,
        name_snapshot=rollout.definition.name,
        requested_by=rollout.created_by,
        rollout=rollout,
        ring=rollout.current_ring,
        host_count=len(hosts),
        step_count=len(actions),
        state=TaskRun.State.RUNNING,
    )
    for host in hosts:
        Task.objects.create(
            host=host,
            requested_by=rollout.created_by,
            run=run,
            step_order=0,
            step_label=rollout.definition.name,
            action="_script",
            params={"steps": steps_payload,
                    "variables": spec.get("resolved_inputs") or {}},
            risk_level=risk,
            state=Task.State.PENDING,
            nonce=secrets.token_hex(32),
            schedule=schedule_snapshot,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay,
        )
    return len(hosts)


def _ring_stats(rollout: PatchRollout) -> dict:
    """total / reported / failed over the tasks dispatched for the
    *current* ring only (all its runs — a resumed ring keeps its history).
    Earlier rings' results must not bleed into this ring's failure gate."""
    tasks = Task.objects.filter(
        run__rollout=rollout, run__ring=rollout.current_ring, step_order=0
    )
    total = tasks.count()
    reported = tasks.filter(state__in=TERMINAL_STATES).count()
    failed = tasks.filter(state__in=FAILURE_STATES).count()
    return {"total": total, "reported": reported, "failed": failed}


def evaluate_rollout(rollout: PatchRollout) -> None:
    """Advance one rollout one step, or halt it.

    Safe to call concurrently: the evaluation runs under
    ``select_for_update`` on the rollout row, so two beat ticks cannot both
    advance the same rollout and double-dispatch a ring.
    """
    if rollout.state not in (
        PatchRollout.State.RUNNING,
        PatchRollout.State.SOAKING,
    ):
        return

    with transaction.atomic():
        fresh = PatchRollout.objects.select_for_update().filter(pk=rollout.pk).first()
        if fresh is None:
            return
        if fresh.state not in (
            PatchRollout.State.RUNNING,
            PatchRollout.State.SOAKING,
        ):
            return
        _evaluate_locked(fresh)


def _evaluate_locked(rollout: PatchRollout) -> None:
    """The state machine. Caller holds the row lock (or is a test)."""
    ring = rollout.current_ring
    if ring is None:
        return

    # 2. Gather the tasks dispatched for the current ring.
    stats = _ring_stats(rollout)

    # 3. Ring incomplete — some task is still pending / dispatched /
    # executing. Wait for the agents to report.
    if stats["total"] - stats["reported"] > 0:
        return

    # 4. + 5. Failure gate, over completed (terminal) tasks only.
    total = stats["reported"]
    failed = stats["failed"]
    pct = (failed * 100) / total if total else 0.0
    if total >= rollout.min_results_before_halt and pct > rollout.failure_threshold_pct:
        # STRICTLY greater than, deliberately: a threshold of 10 with
        # exactly 10% failures proceeds. Do not "fix" this to >= — that
        # would halt rollouts right at the threshold.
        _halt(
            rollout,
            f"ring {ring.name}: {failed}/{total} tasks failed "
            f"({pct:.1f}%) exceeds the {rollout.failure_threshold_pct}% "
            f"failure threshold",
        )
        return

    # 6. The ring passed — soak before the next one may start.
    if rollout.ring_started_at is not None and ring.soak_hours:
        soak_until = rollout.ring_started_at + timedelta(hours=ring.soak_hours)
        if _now() < soak_until:
            if rollout.state != PatchRollout.State.SOAKING:
                rollout.state = PatchRollout.State.SOAKING
                rollout.save(update_fields=["state"])
            return

    # 7. Soaked — find the next enabled ring, dispatch, keep running.
    nxt = _next_enabled_ring(ring.order)
    if nxt is None:
        rollout.state = PatchRollout.State.COMPLETED
        rollout.finished_at = _now()
        rollout.save(update_fields=["state", "finished_at"])
        return

    rollout.current_ring = nxt
    rollout.ring_started_at = _now()
    rollout.state = PatchRollout.State.RUNNING
    rollout.save(update_fields=["current_ring", "ring_started_at", "state"])
    try:
        spec = _validate_definition(rollout.definition)
        _dispatch_ring(rollout, spec)
    except ValueError as exc:
        # The definition was valid at start; a failure here means the
        # definition changed under the rollout. Halt loudly rather than
        # silently stop covering the fleet.
        _halt(rollout, f"dispatch failed for ring {nxt.name}: {exc}")


def _halt(rollout: PatchRollout, reason: str) -> None:
    rollout.state = PatchRollout.State.HALTED
    rollout.halted_reason = reason
    rollout.save(update_fields=["state", "halted_reason"])


def halt_rollout(rollout: PatchRollout, user=None, reason: str = "") -> None:
    """Operator stop: halt now. Only pending/running/soaking can halt —
    a completed or cancelled rollout is already finished."""
    if rollout.state not in (
        PatchRollout.State.PENDING,
        PatchRollout.State.RUNNING,
        PatchRollout.State.SOAKING,
    ):
        raise ValueError("rollout is not active")
    rollout.state = PatchRollout.State.HALTED
    rollout.halted_reason = reason or "halted by operator"
    rollout.halted_by = user
    rollout.save(update_fields=["state", "halted_reason", "halted_by"])


def resume_rollout(rollout: PatchRollout, user=None) -> None:
    """Clear a halt and restart the current ring.

    Failed / rejected / expired tasks on the ring are reset to PENDING with
    fresh nonces so the agents retry them; tasks that were dispatched but
    never reported are left alone (the expiry sweep handles those). The
    gate re-evaluates over the whole ring on the next tick.
    """
    if rollout.state != PatchRollout.State.HALTED:
        raise ValueError("rollout is not halted")
    if rollout.current_ring is None:
        raise ValueError("rollout has no current ring to resume")

    bad = (
        Task.objects.filter(
            run__rollout=rollout, run__ring=rollout.current_ring, step_order=0
        )
        .filter(state__in=FAILURE_STATES)
    )
    for task in bad:
        task.state = Task.State.PENDING
        task.nonce = secrets.token_hex(32)
        task.signature = ""
        task.dispatched_at = None
        task.completed_at = None
        prior = (task.result_output or "").rstrip()
        task.result_output = f"{prior}\n[rolled back for retry after resume]".strip()
        task.save(update_fields=[
            "state", "nonce", "signature", "dispatched_at", "completed_at",
            "result_output",
        ])

    rollout.state = PatchRollout.State.RUNNING
    rollout.halted_reason = ""
    rollout.resumed_by = user
    rollout.save(update_fields=["state", "halted_reason", "resumed_by"])
