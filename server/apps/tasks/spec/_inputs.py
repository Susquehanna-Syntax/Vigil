"""apps.tasks.spec: shared helpers (inputs), moved unchanged."""
from ._common import *  # noqa: F401,F403
from ._params import *  # noqa: F401,F403
from ._flow import *  # noqa: F401,F403


def coerce_inputs(declared: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Coerce *values* against a declared ``inputs:`` list.

    The type/choice/required half of :func:`resolve_inputs`, split out so a
    playbook (whose inputs live on the Playbook row, not in a task spec) can
    validate a caller's values the same way a task does. A declaration written
    by hand may leave out the keys the task YAML's validator always fills in,
    so they are read with their canonical defaults. Raises :class:`SpecError`
    on a missing required input or a bad value.
    """
    resolved: dict[str, Any] = {}
    for inp in declared:
        iid = inp["id"]
        itype = inp.get("type") or "text"
        supplied = values.get(iid, inp.get("default"))

        if itype == "boolean":
            resolved[iid] = bool(supplied)
        elif itype == "number":
            if isinstance(supplied, bool) or not isinstance(supplied, (int, float)):
                try:
                    supplied = float(supplied)
                except (TypeError, ValueError):
                    raise SpecError(f"input {iid!r}: must be a number")
            resolved[iid] = supplied
        elif itype == "choice":
            sval = str(supplied) if supplied is not None else ""
            valid = {c["value"] for c in inp.get("choices") or []}
            if sval not in valid:
                raise SpecError(f"input {iid!r}: {sval!r} is not a valid choice")
            resolved[iid] = sval
        else:  # text
            sval = "" if supplied is None else str(supplied)
            if inp.get("required", True) and not sval:
                raise SpecError(f"input {iid!r} is required")
            if len(sval) > 500:
                raise SpecError(f"input {iid!r}: value too long (max 500)")
            resolved[iid] = sval
    return resolved


def substitute_refs(text: str, values: dict[str, Any]) -> str:
    """Replace every ``${{ inputs.x }}`` in *text* with ``str(values[x])``.

    A value is data: any ``${{`` inside it is written as the escaped ``$${{``
    so the agent's templater leaves it literal, exactly as :func:`resolve_inputs`
    does. Raises :class:`SpecError` on a reference with no value.
    """
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise SpecError(f"unknown input reference ${{{{ inputs.{name} }}}}")
        return str(values[name]).replace("${{", "$${{")

    return _INPUT_REF.sub(repl, text)


def resolve_inputs(parsed_spec: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Substitute supplied input values into action params.

    Returns a copy of ``parsed_spec`` with ``actions[*].params`` rewritten so
    that every ``${{ inputs.x }}`` placeholder is replaced with the resolved
    value. Raises :class:`SpecError` if a required input is missing or a value
    fails type/choice validation.
    """
    declared = parsed_spec.get("inputs") or []
    if not declared:
        return parsed_spec

    resolved = coerce_inputs(declared, values)

    def _sub(value: Any) -> Any:
        if isinstance(value, str):
            def repl(m: re.Match) -> str:
                # A value is data: any ${{ inside it is written as the escaped
                # $${{ so the agent's templater leaves it literal.
                return str(resolved[m.group(2)]).replace("${{", "$${{")
            # One pass for both forms: a second pass would rescan the values
            # the first one inserted and expand markers typed into an input.
            return _ANY_INPUT_REF.sub(repl, value)
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    def _relevant_sub(node: Any) -> Any:
        return {
            "op": node["op"],
            "items": [
                {"probe": {"id": p["probe"]["id"], "type": p["probe"]["type"],
                           "params": _sub(p["probe"]["params"])},
                 "risk": p["risk"]}
                if "probe" in p
                else _relevant_sub(p)
                for p in node["items"]
            ],
        }

    new_actions = []
    for action in parsed_spec.get("actions", []):
        params = action.get("params") or {}
        if action.get("type") == "execute_script" and "script" in params:
            new_params = {
                k: (v if k == "script" else _sub(v)) for k, v in params.items()
            }
        else:
            new_params = _sub(params)
        new_actions.append({**action, "params": new_params})

    new_sc = parsed_spec.get("success_criteria")
    if new_sc and isinstance(new_sc, dict):
        new_sc = dict(new_sc)
        if "output_contains" in new_sc and isinstance(new_sc["output_contains"], str):
            new_sc["output_contains"] = _sub(new_sc["output_contains"])
        if "output_regex" in new_sc and isinstance(new_sc["output_regex"], str):
            new_sc["output_regex"] = _sub(new_sc["output_regex"])

    return {**parsed_spec, "actions": new_actions, "success_criteria": new_sc,
            "relevant": _relevant_sub(parsed_spec["relevant"])
            if parsed_spec.get("relevant") else None,
            "resolved_inputs": resolved}


__all__ = ["coerce_inputs", "resolve_inputs", "substitute_refs"]
