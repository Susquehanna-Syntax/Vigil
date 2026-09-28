"""Shared spec builders for deploying a task definition as a signed task.

Both the manual deploy endpoint and the playbook dispatcher build a
definition's signed task params the same way: expand ``use:`` references,
resolve inputs, expand inline ``type: playbook`` actions, and sign the steps
with the task's own step ids and flow / relevant: tree. This module is that
shared path (phase 08a); ``definition_deploy`` and ``rollout`` keep their
own copies until 08a2 moves them here.
"""

from __future__ import annotations

from datetime import datetime

from .spec import (
    SpecError,
    _deploy_params,
    _max_risk,
    hunt_expiry,
    parse_and_validate,
    resolve_inputs,
)
from .uses import UseError, expand_uses


def resolve_task_spec(
    definition, *, user, inputs: dict | None = None, params_override: dict | None = None
) -> dict:
    """The definition's parsed spec with ``use:`` expanded, inputs resolved
    and the per-action ``params_override`` applied.

    ``params_override`` maps stringified action indexes to param dicts and is
    merged over each action's params the way a direct deploy would. Raises
    ``SpecError`` / ``UseError`` when the definition cannot be built.
    """
    from apps.playbooks.expansion import expand_actions

    spec = definition.parsed_spec or {}
    if spec.get("uses"):
        # Re-derived on every dispatch, so the used tasks' steps ship as
        # they are now (a run already dispatched is unaffected).
        copied: list[dict] = []
        try:
            spec = parse_and_validate(
                expand_uses(definition.yaml_source, user, audit=copied)
            )
        except UseError as exc:
            raise SpecError(str(exc)) from exc
        spec["uses_copied"] = copied
    spec = resolve_inputs(spec, inputs or {})

    if params_override:
        spec["actions"] = [
            {
                **action,
                "params": {
                    **(action.get("params") or {}),
                    **params_override.get(str(index), {}),
                },
            }
            for index, action in enumerate(spec.get("actions") or [])
        ]

    actions, _expanded_risk = expand_actions(spec.get("actions") or [])
    spec["actions"] = actions
    return spec


def task_params(
    spec: dict, now: datetime | None = None
) -> tuple[dict, str, datetime | None]:
    """``(signed params, risk, expires_at)`` for a resolved spec.

    Builds ``steps_payload`` exactly as ``definition_deploy`` does — each
    action keeps its own ``id`` so the task's ``when:`` and
    ``${{ steps.<id> }}`` references name real steps — and signs it with
    ``_deploy_params``.
    """
    from django.utils.timezone import now as _now

    from apps.playbooks.expansion import expand_actions

    actions, expanded_risk = expand_actions(spec.get("actions") or [])
    if not actions:
        raise SpecError("definition has no actions")

    success = spec.get("success_criteria") or None
    steps_payload = []
    for index, action in enumerate(actions):
        step = {
            "id": action.get("id") or f"step{index + 1}",
            "action": action["type"],
            "params": action.get("params") or {},
        }
        when_expr = action.get("when") or ""
        if when_expr:
            step["when"] = when_expr
        if action.get("timeout"):
            step["timeout"] = action["timeout"]
        if success:
            step["success_criteria"] = success
        steps_payload.append(step)

    # update_agent replaces the whole agent executable: stamp the verified
    # SHA-256 of each platform binary so the agent can check the download
    # against a digest carried inside this signed task (a TLS-only transfer
    # is not a strong enough proof for that swap).
    if any(s["action"] == "update_agent" for s in steps_payload):
        from apps.agent_dist.views import all_binary_sha256

        sha_map = all_binary_sha256()
        for s in steps_payload:
            if s["action"] == "update_agent":
                s["params"] = {**(s.get("params") or {}), "binary_sha256": sha_map}

    risk = _max_risk(spec.get("risk", "standard"), expanded_risk)
    expires_at = hunt_expiry(steps_payload, _now() if now is None else now)
    return _deploy_params(spec, steps_payload), risk, expires_at


def build_playbook_steps(playbook, *, user) -> list[tuple]:
    """Every step of *playbook*, built as its own signed task.

    Returns ``[(playbook_step, params, risk, expires_at), …]`` in order.
    Raises (``SpecError`` / ``PlaybookExpandError``) if any step cannot be
    built, so a caller never writes half a chain.
    """
    built = []
    for step in playbook.steps.select_related("definition").order_by("order"):
        spec = resolve_task_spec(step.definition, user=user,
                                 params_override=step.params_override)
        params, risk, expires_at = task_params(spec)
        built.append((step, params, risk, expires_at))
    return built


def create_chain(run, host, built, *, requested_by, label: str) -> list:
    """One task per built playbook step on *host*, chained.

    Every task starts blocked and carries its playbook step id, branch path
    and — inside an if/then/else — its guard (M6 08b); then the chain is
    released: the server walks the blocked tasks in order, skipping any whose
    guard is false, and makes the first runnable one pending.
    """
    import secrets

    from apps.playbooks.flow import branch_guards

    from .models import Task
    from .views import _release_next

    flow = built[0][0].playbook.flow if built else None
    guards = branch_guards(flow)
    if built and run.flow_snapshot is None:
        run.flow_snapshot = {
            "flow": flow,
            "steps": [{"step_id": step.step_id, "definition_id": str(step.definition_id),
                       "definition_name": step.definition.name, "risk": risk,
                       "color": step.color, "outcome": step.outcome,
                       "on_not_applicable": step.on_not_applicable,
                       "on_failure": step.on_failure}
                      for step, _params, risk, _expires in built],
        }
        run.save(update_fields=["flow_snapshot"])
    tasks = []
    for i, (step, params, risk, expires_at) in enumerate(built):
        guard, branch = guards.get(step.step_id, ("", ""))
        tasks.append(Task.objects.create(
            host=host,
            run=run,
            requested_by=requested_by,
            step_label=f"{label} → {step.definition.name}",
            action="_script",
            params=params,
            risk_level=risk,
            state=Task.State.BLOCKED,
            step_order=i,
            expires_at=expires_at,
            on_not_applicable=step.on_not_applicable,
            on_failure=step.on_failure,
            step_ref=step.step_id,
            branch=branch,
            guard=guard,
            nonce=secrets.token_hex(32),
        ))
    if tasks:
        _release_next(run, host)
    return tasks


def highest_risk(built) -> str:
    risk = "low"
    for _step, _params, step_risk, _expires in built:
        risk = _max_risk(risk, step_risk)
    return risk
