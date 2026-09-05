"""Staged rollout engine: the advance/halt brain for ``PatchRollout``.

Pure-ish over database state so it can be called from the beat task, from a
task-result webhook, and from tests. The one clock read goes through
``_now()`` so tests can patch the validation window without sleeping.
"""

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from .models import (
    PatchWave,
    PatchRollout,
    Task,
    TaskRun,
    rollout_wave_plan,
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


def _first_enabled_wave():
    return PatchWave.objects.filter(enabled=True).order_by("order", "id").first()


def _next_enabled_wave(after_order):
    return (
        PatchWave.objects.filter(enabled=True, order__gt=after_order)
        .order_by("order", "id")
        .first()
    )


def _validate_definition(definition) -> dict:
    """Prove the definition can be dispatched without operator inputs.

    A rollout runs unattended across waves; a definition with required
    inputs has no value to fill them with, so it is refused at start rather
    than discovered mid-rollout. Returns the resolved spec.
    """
    spec = definition.parsed_spec or {}
    try:
        return resolve_inputs(spec, {})
    except SpecError as exc:
        raise ValueError(f"definition {definition.name!r} cannot roll out: {exc}") from exc


def _playbook_spec(playbook) -> dict:
    """A one-action spec that expands into the playbook's steps.

    `expand_actions` already inlines a `type: playbook` action, with cycle
    detection and a depth limit, so rolling out a playbook needs no separate
    expansion path — it reuses the same composition the task editor uses.
    """
    return {
        "name": playbook.name,
        "risk": "high" if playbook.allow_high_risk else "standard",
        "actions": [{"type": "playbook", "params": {"name": playbook.name}}],
    }


def rollout_spec(rollout) -> dict:
    """Re-derive the spec for a rollout, whichever target it carries.

    Called on every wave advance, not just at start — a playbook rollout has no
    `definition`, so anything that reaches for `rollout.definition` directly
    breaks on wave 2 rather than wave 1, which is a nasty place to find out.
    """
    if rollout.action_kind == PatchRollout.ActionKind.PLAYBOOK:
        if rollout.playbook is None:
            raise ValueError("rollout's playbook no longer exists")
        return _playbook_spec(rollout.playbook)
    if rollout.definition is None:
        raise ValueError("rollout's task definition no longer exists")
    return _validate_definition(rollout.definition)


def start_rollout(
    definition=None,
    user=None,
    failure_threshold_pct: int = 10,
    min_results_before_halt: int = 3,
    playbook=None,
) -> PatchRollout:
    """Create a rollout and dispatch its first wave.

    Raises ``ValueError`` when no enabled wave exists, the definition
    cannot be dispatched without operator inputs, or the gate settings are
    out of range.
    """
    if not 0 <= failure_threshold_pct <= 100:
        raise ValueError("failure_threshold_pct must be between 0 and 100")
    if min_results_before_halt < 1:
        raise ValueError("min_results_before_halt must be at least 1")
    if _first_enabled_wave() is None:
        raise ValueError("no enabled patch waves")
    if (definition is None) == (playbook is None):
        raise ValueError("a rollout needs exactly one of a definition or a playbook")

    if playbook is not None:
        # Deliberately no auto_enroll check. That flag governs unattended
        # enrolment; a rollout is the opposite — someone chose this playbook
        # and is staging it wave by wave. Refusing here made the recommended
        # path impossible for the recommended configuration.
        spec = _playbook_spec(playbook)
    else:
        spec = _validate_definition(definition)

    ts = _now()
    rollout = PatchRollout.objects.create(
        definition=definition,
        playbook=playbook,
        action_kind=(PatchRollout.ActionKind.PLAYBOOK if playbook is not None
                     else PatchRollout.ActionKind.TASK),
        state=PatchRollout.State.RUNNING,
        current_wave=_first_enabled_wave(),
        failure_threshold_pct=failure_threshold_pct,
        min_results_before_halt=min_results_before_halt,
        started_at=ts,
        wave_started_at=ts,
        created_by=user,
    )
    with transaction.atomic():
        _dispatch_wave(rollout, spec)
    return rollout


def _dispatch_wave(rollout: PatchRollout, spec: dict) -> int:
    """Dispatch one task per wave host and open a TaskRun for them.

    Hosts come from the cross-wave plan, not the raw wave membership: a
    host tagged into two waves belongs to the earliest wave only, so this
    wave must not re-claim it. Must run inside a transaction that holds
    the rollout row lock (see ``evaluate_rollout``) — otherwise two beat
    ticks can both pass the "no run yet for this wave" check and dispatch
    the same wave twice. An empty wave still opens a run (zero hosts):
    the gate then sees 0/0, passes, validates, and advances.
    Returns the number of tasks created.
    """
    from apps.playbooks.expansion import _max_risk, expand_actions
    from apps.hosts.models import Host

    enabled = list(
        PatchWave.objects.filter(enabled=True).order_by("order", "id")
    )
    plan = rollout_wave_plan(enabled)
    host_ids = plan.get(rollout.current_wave.id, [])
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
        definition=rollout.definition,   # None for a playbook rollout
        name_snapshot=rollout.target_name,
        requested_by=rollout.created_by,
        rollout=rollout,
        wave=rollout.current_wave,
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
            step_label=rollout.target_name,
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


def _wave_stats(rollout: PatchRollout) -> dict:
    """total / reported / failed over the tasks dispatched for the
    *current* wave only (all its runs — a resumed wave keeps its history).
    Earlier waves' results must not bleed into this wave's failure gate."""
    tasks = Task.objects.filter(
        run__rollout=rollout, run__wave=rollout.current_wave, step_order=0
    )
    total = tasks.count()
    reported = tasks.filter(state__in=TERMINAL_STATES).count()
    failed = tasks.filter(state__in=FAILURE_STATES).count()
    return {"total": total, "reported": reported, "failed": failed}


def evaluate_rollout(rollout: PatchRollout) -> None:
    """Advance one rollout one step, or halt it.

    Safe to call concurrently: the evaluation runs under
    ``select_for_update`` on the rollout row, so two beat ticks cannot both
    advance the same rollout and double-dispatch a wave.
    """
    if rollout.state not in (
        PatchRollout.State.RUNNING,
        PatchRollout.State.VALIDATING,
    ):
        return

    with transaction.atomic():
        fresh = PatchRollout.objects.select_for_update().filter(pk=rollout.pk).first()
        if fresh is None:
            return
        if fresh.state not in (
            PatchRollout.State.RUNNING,
            PatchRollout.State.VALIDATING,
        ):
            return
        _evaluate_locked(fresh)


def _evaluate_locked(rollout: PatchRollout) -> None:
    """The state machine. Caller holds the row lock (or is a test)."""
    wave = rollout.current_wave
    if wave is None:
        return

    # 2. Gather the tasks dispatched for the current wave.
    stats = _wave_stats(rollout)

    # 3. Wave incomplete — some task is still pending / dispatched /
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
            f"wave {wave.name}: {failed}/{total} tasks failed "
            f"({pct:.1f}%) exceeds the {rollout.failure_threshold_pct}% "
            f"failure threshold",
        )
        return

    # 6. The wave passed — validation before the next one may start.
    if rollout.wave_started_at is not None and wave.validation_hours:
        validation_until = rollout.wave_started_at + timedelta(hours=wave.validation_hours)
        if _now() < validation_until:
            if rollout.state != PatchRollout.State.VALIDATING:
                rollout.state = PatchRollout.State.VALIDATING
                rollout.save(update_fields=["state"])
            return

    # 7. Soaked — find the next enabled wave, dispatch, keep running.
    nxt = _next_enabled_wave(wave.order)
    if nxt is None:
        rollout.state = PatchRollout.State.COMPLETED
        rollout.finished_at = _now()
        rollout.save(update_fields=["state", "finished_at"])
        return

    rollout.current_wave = nxt
    rollout.wave_started_at = _now()
    rollout.state = PatchRollout.State.RUNNING
    rollout.save(update_fields=["current_wave", "wave_started_at", "state"])
    try:
        spec = rollout_spec(rollout)
        _dispatch_wave(rollout, spec)
    except ValueError as exc:
        # The definition was valid at start; a failure here means the
        # definition changed under the rollout. Halt loudly rather than
        # silently stop covering the fleet.
        _halt(rollout, f"dispatch failed for wave {nxt.name}: {exc}")


def _halt(rollout: PatchRollout, reason: str) -> None:
    rollout.state = PatchRollout.State.HALTED
    rollout.halted_reason = reason
    rollout.save(update_fields=["state", "halted_reason"])


def halt_rollout(rollout: PatchRollout, user=None, reason: str = "") -> None:
    """Operator stop: halt now. Only pending/running/validating can halt —
    a completed or cancelled rollout is already finished."""
    if rollout.state not in (
        PatchRollout.State.PENDING,
        PatchRollout.State.RUNNING,
        PatchRollout.State.VALIDATING,
    ):
        raise ValueError("rollout is not active")
    rollout.state = PatchRollout.State.HALTED
    rollout.halted_reason = reason or "halted by operator"
    rollout.halted_by = user
    rollout.save(update_fields=["state", "halted_reason", "halted_by"])


def skip_validation(rollout: PatchRollout, user=None) -> None:
    """End the current wave's validation window now and advance.

    The window exists so a slow-burn failure has time to surface. Skipping it
    is a judgement call an operator is entitled to make — they watched the
    wave and are satisfied — so this backdates `wave_started_at` past the
    window rather than bypassing the gate. The failure gate still applies:
    a wave that failed its threshold stays halted and this does nothing for it.
    """
    if rollout.state != PatchRollout.State.VALIDATING:
        raise ValueError("this rollout is not in a validation window")
    wave = rollout.current_wave
    hours = wave.validation_hours if wave else 0
    # Backdate rather than special-case the evaluator: every other caller
    # keeps reading one rule for "has the window elapsed".
    rollout.wave_started_at = _now() - timedelta(hours=hours + 1)
    rollout.save(update_fields=["wave_started_at"])
    evaluate_rollout(rollout)


def resume_rollout(rollout: PatchRollout, user=None) -> None:
    """Clear a halt and restart the current wave.

    Failed / rejected / expired tasks on the wave are reset to PENDING with
    fresh nonces so the agents retry them; tasks that were dispatched but
    never reported are left alone (the expiry sweep handles those). The
    gate re-evaluates over the whole wave on the next tick.
    """
    if rollout.state != PatchRollout.State.HALTED:
        raise ValueError("rollout is not halted")
    if rollout.current_wave is None:
        raise ValueError("rollout has no current wave to resume")

    bad = (
        Task.objects.filter(
            run__rollout=rollout, run__wave=rollout.current_wave, step_order=0
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
