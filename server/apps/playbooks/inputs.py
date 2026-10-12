"""Playbook inputs: values from a caller, mapped onto each step's task.

A playbook declares its own ``inputs`` (the task spec's ``inputs:`` shape,
validated on save). Every caller — an automation, a rollout, a task's inline
``type: playbook`` action — supplies values for them; auto-enrolment supplies
none, which is why a playbook with any input cannot auto-enrol.

Each step then maps playbook values onto its task's declared inputs: an entry
in ``PlaybookStep.inputs`` is text with ``${{ inputs.x }}`` references,
substituted against the playbook's values; a task input with no mapping takes
the playbook value carrying the same id; with neither, the task's own default
applies.
"""

from __future__ import annotations

from typing import Any

from apps.tasks.spec import _INPUT_REF, SpecError, coerce_inputs, substitute_refs

MAX_STEP_INPUTS = 16
MAX_STEP_INPUT_LEN = 500


class PlaybookInputError(ValueError):
    """Playbook input values a caller supplied that the playbook cannot use."""


def canonical(declarations) -> list[dict[str, Any]]:
    """Declarations with every key ``coerce_inputs`` reads, defaults included.

    Declarations are authored by hand (a playbook row created in a shell, a
    migration, a test) and a missing ``default`` or ``choices`` key is a
    ``KeyError`` in the middle of a dispatch, not a helpful error.
    """
    out: list[dict[str, Any]] = []
    for inp in declarations or []:
        itype = inp.get("type") or "text"
        default = inp.get("default")
        if default is None:
            default = False if itype == "boolean" else (0 if itype == "number" else "")
        out.append({**inp,
                    "type": itype,
                    "default": default,
                    "choices": inp.get("choices") or [],
                    "required": bool(inp.get("required", itype != "boolean"))})
    return out


def playbook_values(playbook, supplied: dict | None) -> dict[str, Any]:
    """*supplied* coerced against the playbook's declarations, defaults included."""
    try:
        return coerce_inputs(canonical(playbook.inputs), supplied or {})
    except SpecError as exc:
        raise PlaybookInputError(f"playbook {playbook.name!r}: {exc}") from exc


def step_inputs(step, values: dict[str, Any]) -> dict[str, Any]:
    """The step's task-input values, built from the playbook's *values*."""
    declared = [i["id"] for i in (step.definition.parsed_spec or {}).get("inputs") or []]
    mapping = step.inputs or {}
    out: dict[str, Any] = {}
    for iid in declared:
        if iid in mapping:
            out[iid] = substitute_refs(str(mapping[iid]), values)
        elif iid in values:
            out[iid] = values[iid]
    return out


def validate_step_inputs(definition, mapping, playbook_inputs) -> str | None:
    """Check a step's input mapping. Returns an error message or None."""
    if not isinstance(mapping, dict):
        return "inputs must be a mapping of task input id to value"
    if len(mapping) > MAX_STEP_INPUTS:
        return f"at most {MAX_STEP_INPUTS} step inputs"
    declared = {i["id"] for i in (definition.parsed_spec or {}).get("inputs") or []}
    available = {i["id"] for i in playbook_inputs or []}
    for key, value in mapping.items():
        if key not in declared:
            return (f"{key!r} is not an input of {definition.name!r}"
                    if declared else f"{definition.name!r} declares no inputs")
        if not isinstance(value, str):
            return f"input {key!r} must be text"
        if len(value) > MAX_STEP_INPUT_LEN:
            return f"input {key!r} is at most {MAX_STEP_INPUT_LEN} characters"
        for match in _INPUT_REF.finditer(value):
            ref = match.group(1)
            if ref not in available:
                return f"input {key!r} refers to unknown playbook input {ref!r}"
    return None
