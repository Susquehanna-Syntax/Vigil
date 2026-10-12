"""Playbook-as-a-function: expanding ``type: playbook`` actions server-side.

A task definition may contain::

    actions:
      - type: playbook
        params:
          name: Linux bootstrap
          inputs: {domain: "${{ inputs.target_domain }}"}

At deploy/dispatch time the reference is replaced inline with the playbook's
own steps — the agent only ever receives concrete, allowlistable actions and
never sees the ``playbook`` type. Expansion is recursive (playbooks may call
playbooks) with a cycle guard and a depth cap, and the effective risk is the
max across everything that got pulled in.

The playbook's inputs are resolved here, where its declarations are in hand;
the values are then filled into the inlined steps' params by
:func:`resolve_playbook_inputs`, which the caller that started the expansion
runs once over the flat result, with those values only. A supplied value that
is a ``${{ inputs.x }}`` reference into the calling task's inputs was already
substituted by that task's own ``resolve_inputs`` before expansion.
"""

from __future__ import annotations

MAX_DEPTH = 5

_RISK_ORDER = {"low": 0, "standard": 1, "high": 2}


def unsupported_in_playbooks(spec: dict) -> list[str]:
    """M6 task-language constructs a playbook cannot carry yet.

    A playbook flattens its tasks' actions into one step list, which would
    drop a task's branch flow (running every branch) and its relevant: block
    (running the fix on hosts it does not apply to). Until playbooks learn
    them, a task that uses any of these cannot be part of one.
    """
    spec = spec or {}
    found = []
    if spec.get("flow"):
        found.append("if/then/else branches")
    if spec.get("relevant"):
        found.append("relevant:")
    if spec.get("uses"):
        found.append("use:")
    return found


class PlaybookExpandError(ValueError):
    """A playbook reference that cannot be satisfied (unknown name, cycle,
    too deep). Deploy paths surface this as a 400; auto-enroll dispatch logs
    and skips."""


def step_declarations(step, values: dict) -> dict:
    """The step's task inputs, declared with the playbook's value as default.

    A step's actions are inlined into the *caller's* list; the pass that gives
    them their values runs at the end of the whole expansion, so the resolved
    values travel on the actions as pseudo-declarations. ``resolve_inputs``
    then treats each as that input's default and substitutes it once.
    """
    from .inputs import step_inputs

    return {iid: {"id": iid, "type": "text", "label": iid, "description": "",
                  "choices": [], "default": val, "required": False}
            for iid, val in step_inputs(step, values).items()}


def expand_actions(actions: list[dict], *, _seen: frozenset = frozenset(),
                   _depth: int = 0, _outer: dict | None = None
                   ) -> tuple[list[dict], str]:
    """Return ``(concrete_actions, max_risk)`` with playbook refs inlined.

    *values* maps the playbook's name (lowercased) to the values its reference
    supplied, resolved against its declarations. Nested playbooks see the
    enclosing playbook's values for a supplied value that is itself a
    ``${{ inputs.x }}`` reference.
    """
    from apps.tasks.spec import SpecError, substitute_refs

    from .inputs import PlaybookInputError, playbook_values
    from .models import Playbook

    if _depth > MAX_DEPTH:
        raise PlaybookExpandError(f"playbook nesting deeper than {MAX_DEPTH}")

    out: list[dict] = []
    max_risk = "low"

    for action in actions or []:
        if action.get("type") != "playbook":
            out.append(action)
            continue

        name = str((action.get("params") or {}).get("name", "")).strip()
        key = name.lower()
        if not name:
            raise PlaybookExpandError("playbook action missing params.name")
        if key in _seen:
            raise PlaybookExpandError(f"playbook cycle via {name!r}")
        playbook = Playbook.objects.filter(name__iexact=name).first()
        if playbook is None:
            raise PlaybookExpandError(f"no playbook named {name!r}")

        supplied = dict((action.get("params") or {}).get("inputs") or {})
        # A supplied value may name an input of the playbook that runs this
        # one; the declarations that name it are the enclosing playbook's.
        if _outer and any(isinstance(v, str) and "${{" in v for v in supplied.values()):
            try:
                supplied = {k: substitute_refs(str(v), _outer) if isinstance(v, str) else v
                            for k, v in supplied.items()}
            except SpecError as exc:
                raise PlaybookExpandError(str(exc)) from exc
        try:
            values = playbook_values(playbook, supplied)
        except (PlaybookInputError, SpecError) as exc:
            raise PlaybookExpandError(str(exc)) from exc

        for step in playbook.steps.select_related("definition").order_by("order"):
            spec = step.definition.parsed_spec or {}
            blocked = unsupported_in_playbooks(spec)
            if blocked:
                raise PlaybookExpandError(
                    f"task {step.definition.name!r} in playbook {name!r} uses "
                    f"{', '.join(blocked)}, which playbooks cannot run yet")
            override = step.params_override or {}
            actions_with_override = [
                {**act, "params": {**(act.get("params") or {}),
                                   **override.get(str(i), {})}}
                for i, act in enumerate(spec.get("actions") or [])
            ] if override else (spec.get("actions") or [])
            inner, inner_risk = expand_actions(
                actions_with_override,
                _seen=_seen | {key}, _depth=_depth + 1,
                _outer=values,
            )
            declarations = step_declarations(step, values)
            out.extend({**act, "_playbook_inputs": declarations}
                       for act in inner)
            max_risk = _max_risk(max_risk, spec.get("risk", "standard"))
            max_risk = _max_risk(max_risk, inner_risk)

    return out, max_risk


def resolve_playbook_inputs(actions: list[dict]) -> list[dict]:
    """Fill the input references the inlined steps still carry.

    Run once, by the task that started the expansion. Each inlined action
    carries the values its playbook resolved (and validated) for its step;
    those are the only values substituted. The calling task's own inputs
    were already substituted into the playbook reference before expansion,
    so nothing from the request is consulted here.
    """
    from apps.tasks.spec import resolve_inputs

    for action in actions:
        declared = action.pop("_playbook_inputs", None)
        if declared:
            action["params"] = resolve_inputs(
                {"inputs": list(declared.values()), "actions": [action]},
                {})["actions"][0]["params"]
    return actions


def _max_risk(a: str, b: str) -> str:
    return a if _RISK_ORDER.get(a, 1) >= _RISK_ORDER.get(b, 1) else b
