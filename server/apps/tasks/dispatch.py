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
    from apps.playbooks.expansion import (expand_actions, resolve_playbook_inputs)

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
    # A playbook's steps arrive carrying the values that playbook resolved.
    # This task's own inputs were substituted into the playbook action's
    # params.inputs above, before expansion, and checked against this task's
    # declarations there. The raw request is never consulted again: a key in
    # it named like a step's task input must not replace what the playbook
    # resolved and validated.
    spec["actions"] = resolve_playbook_inputs(actions)
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


def build_playbook_steps(playbook, *, user, inputs: dict | None = None) -> list[tuple]:
    """Every step of *playbook*, built as its own signed task.

    *inputs* are the caller's values for the playbook's own inputs (an
    automation, a rollout, a task's inline playbook action); ``None`` — what
    auto-enrolment passes — means "nobody is here to fill anything in", which
    works only when every required input has a default.

    Returns ``[(playbook_step, params, risk, expires_at), …]`` in order.
    Raises (``SpecError`` / ``PlaybookExpandError`` / ``PlaybookInputError``)
    if any step cannot be built, so a caller never writes half a chain.
    """
    from apps.playbooks.inputs import playbook_values, step_inputs

    values = playbook_values(playbook, inputs)
    built = []
    for step in playbook.steps.select_related("definition").order_by("order"):
        spec = resolve_task_spec(step.definition, user=user,
                                 inputs=step_inputs(step, values),
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

    from apps.playbooks.flow import branch_guards, flow_sequence

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
    # A go to (QA-36) is a task of its own in the chain, resolved by the
    # server in _release_next and never sent to the agent. The returned list
    # holds the step tasks only: callers count it as the steps.
    # Only a chain of one playbook's steps follows its flow; a chain built
    # from several playbooks (whose step ids may repeat) keeps built order.
    sequence = [{"built": b} for b in built]
    if len({step.playbook_id for step, *_rest in built}) == 1:
        by_sid = {b[0].step_id: b for b in built}
        flowed = [n if "goto" in n else {"built": by_sid[n["step"]]}
                  for n in flow_sequence(flow) if "goto" in n or n["step"] in by_sid]
        if any("goto" in n for n in flowed) and len(flowed) - sum(
                "goto" in n for n in flowed) == len(built):
            sequence = flowed
    tasks = []
    for i, node in enumerate(sequence):
        if "goto" in node:
            guard, branch = guards.get("@" + node["id"], ("", ""))
            Task.objects.create(
                host=host,
                run=run,
                requested_by=requested_by,
                step_label=f"{label} → go to {node['goto']}"[:120],
                action="_goto",
                params={"goto": node["goto"], "goto_id": node["id"], "max": node["max"]},
                risk_level="low",
                state=Task.State.BLOCKED,
                step_order=i,
                branch=branch,
                guard=guard,
                nonce=secrets.token_hex(32),
            )
            continue
        step, params, risk, expires_at = node["built"]
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
