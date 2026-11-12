"""apps.tasks.spec: shared helpers (flow), moved unchanged."""
from ._common import *  # noqa: F401,F403
from ._params import *  # noqa: F401,F403


def _validate_schedule(raw: Any) -> dict[str, Any] | None:
    """Validate the optional ``schedule`` block.

    Schema::

        schedule:
          window:
            start_hour:   8   # 0..23 inclusive
            start_minute: 30  # 0..59, default 0
            end_hour:    17   # 0..23 inclusive
            end_minute:   0   # 0..59, default 0
            days: [mon, tue, wed, thu, fri]   # optional, defaults to all 7

    Returns a canonical ``{"window": {...}}`` dict or ``None`` if no schedule
    block is provided. ``end_hour``/``end_minute`` is inclusive — a window of
    08:00..17:00 means a task may dispatch any time from 08:00 through 17:59.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("'schedule' must be a mapping")

    window = raw.get("window")
    if window is None:
        return None
    if not isinstance(window, dict):
        raise SpecError("'schedule.window' must be a mapping")

    def _hour(name: str, default: int) -> int:
        v = window.get(name, default)
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise SpecError(f"schedule.window.{name} must be an integer 0..23")
        if not 0 <= v <= 23:
            raise SpecError(f"schedule.window.{name} must be between 0 and 23")
        return v

    def _minute(name: str, default: int = 0) -> int:
        v = window.get(name, default)
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise SpecError(f"schedule.window.{name} must be an integer 0..59")
        if not 0 <= v <= 59:
            raise SpecError(f"schedule.window.{name} must be between 0 and 59")
        return v

    start_hour = _hour("start_hour", 0)
    start_minute = _minute("start_minute", 0)
    end_hour = _hour("end_hour", 23)
    end_minute = _minute("end_minute", 0)

    raw_days = window.get("days")
    if raw_days is None:
        days = list(_ALL_DAYS)
    else:
        if not isinstance(raw_days, list) or not raw_days:
            raise SpecError("schedule.window.days must be a non-empty list")
        days = []
        for entry in raw_days:
            if isinstance(entry, int) and not isinstance(entry, bool):
                if not 0 <= entry <= 6:
                    raise SpecError(f"day index must be 0..6, got {entry}")
                day_idx = entry
            elif isinstance(entry, str):
                key = entry.strip().lower()
                if key not in _DAY_ALIASES:
                    raise SpecError(
                        f"unknown day {entry!r} — expected mon..sun or 0..6"
                    )
                day_idx = _DAY_ALIASES[key]
            else:
                raise SpecError(f"day entries must be names or 0..6 ints")
            if day_idx not in days:
                days.append(day_idx)
        days.sort()

    return {
        "window": {
            "start_hour": start_hour,
            "start_minute": start_minute,
            "end_hour": end_hour,
            "end_minute": end_minute,
            "days": days,
        }
    }


def _validate_on_failure(raw: Any) -> dict[str, Any] | None:
    """Validate the optional ``on_failure`` block.

    Schema::

        on_failure:
          retry:
            attempts: 3        # 0..10 — max retries after the first attempt
            delay_seconds: 60  # 0..3600 — delay between attempts (default 30)
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("'on_failure' must be a mapping")

    retry = raw.get("retry")
    if retry is None:
        return None
    if not isinstance(retry, dict):
        raise SpecError("'on_failure.retry' must be a mapping")

    attempts = retry.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        try:
            attempts = int(attempts)
        except (TypeError, ValueError):
            raise SpecError("on_failure.retry.attempts must be a non-negative integer")
    if not 0 <= attempts <= 10:
        raise SpecError("on_failure.retry.attempts must be between 0 and 10")

    delay = retry.get("delay_seconds", 30)
    if isinstance(delay, bool) or not isinstance(delay, int):
        try:
            delay = int(delay)
        except (TypeError, ValueError):
            raise SpecError("on_failure.retry.delay_seconds must be an integer")
    if not 0 <= delay <= 3600:
        raise SpecError("on_failure.retry.delay_seconds must be between 0 and 3600")

    return {"retry": {"attempts": attempts, "delay_seconds": delay}}


def _validate_success_criteria(raw: Any) -> dict[str, Any] | None:
    """Validate the optional ``success_criteria`` block.

    Schema::

        success_criteria:
          exit_code: 0                # default 0
          output_contains: "OK"       # optional substring match
          output_regex: "^DONE"       # optional regex (compiled here to surface
                                      # invalid patterns at validation time)
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("'success_criteria' must be a mapping")

    canonical: dict[str, Any] = {}

    if "exit_code" in raw:
        ec = raw.get("exit_code")
        if isinstance(ec, bool) or not isinstance(ec, int):
            try:
                ec = int(ec)
            except (TypeError, ValueError):
                raise SpecError("success_criteria.exit_code must be an integer")
        canonical["exit_code"] = ec

    if "output_contains" in raw:
        oc = raw.get("output_contains")
        if oc is None:
            oc = ""
        elif not isinstance(oc, str):
            raise SpecError("success_criteria.output_contains must be a string")
        if len(oc) > 500:
            raise SpecError("success_criteria.output_contains too long (max 500)")
        if oc:
            canonical["output_contains"] = oc

    if "output_regex" in raw:
        rx = raw.get("output_regex")
        if rx is None:
            rx = ""
        elif not isinstance(rx, str):
            raise SpecError("success_criteria.output_regex must be a string")
        if len(rx) > 500:
            raise SpecError("success_criteria.output_regex too long (max 500)")
        if rx:
            if "{{" not in rx:
                try:
                    re.compile(rx)
                except re.error as exc:
                    raise SpecError(f"success_criteria.output_regex invalid: {exc}")
            canonical["output_regex"] = rx

    return canonical or None


def schedule_window_active(
    schedule: dict[str, Any] | None, *, weekday: int, hour: int, minute: int = 0
) -> bool:
    """Return True if *schedule* allows dispatch at the given weekday/time.

    Used by the dispatch path (hosts/views/agent.py) to decide whether a pending
    task is eligible for immediate handoff. ``weekday`` follows the Python
    convention (0 = Monday). ``hour`` is 0..23, ``minute`` is 0..59.

    A task with no schedule (``None``) is always active. The end boundary is
    inclusive at the hour level: a 08:00–17:00 window includes 17:59:59.
    """
    if not schedule:
        return True
    window = schedule.get("window") if isinstance(schedule, dict) else None
    if not window:
        return True

    days = window.get("days") or list(_ALL_DAYS)
    if weekday not in days:
        return False

    start = int(window.get("start_hour", 0)) * 60 + int(window.get("start_minute", 0))
    end   = int(window.get("end_hour", 23))  * 60 + int(window.get("end_minute", 0))
    now_m = hour * 60 + minute
    if start <= end:
        return start <= now_m <= end + 59  # end hour is inclusive through :59
    # Wrap-around window (e.g. 22:00..06:00 — overnight maintenance)
    return now_m >= start or now_m <= end + 59


def _max_risk(a: str, b: str) -> str:
    """The higher of two risk tiers — the composite risk of a task that
    inlines another task's steps (phase 05b) is the max across both."""
    return a if _RISK_ORDER.get(a, 1) >= _RISK_ORDER.get(b, 1) else b


def _check_step_ref(
    step_id: str, field: str | None, earlier: dict[str, str], where: str
) -> None:
    """A step reference must name an earlier step and, for a result, a declared output.

    ``field`` is None for ``steps.<id>.status``, which every step has. The
    value only exists on the host at run time, so this is the one place a bad
    reference can be caught before a task is signed.
    """
    if step_id not in earlier:
        raise SpecError(f"{where}: steps.{step_id} is not an earlier step")
    if field is not None:
        declared = action_outputs(earlier[step_id])
        if field not in declared:
            raise SpecError(
                f"{where}: {earlier[step_id]} step {step_id!r} has no output "
                f"{field!r}; declared: {sorted(declared)}"
            )


def _validate_action_inputs(value: Any) -> dict[str, Any]:
    """The optional ``inputs`` mapping of a ``type: playbook`` action."""
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        raise SpecError("playbook action: 'inputs' must be a mapping of "
                        "playbook input id to value")
    if len(value) > MAX_ACTION_INPUTS:
        raise SpecError(f"playbook action: 'inputs' has at most "
                        f"{MAX_ACTION_INPUTS} entries")
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SpecError("playbook action: 'inputs' keys must be names")
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise SpecError(f"playbook action: input {key!r} must be text, "
                            "a number, or true/false")
        if isinstance(val, str) and len(val) > MAX_ACTION_INPUT_LEN:
            raise SpecError(f"playbook action: input {key!r} is at most "
                            f"{MAX_ACTION_INPUT_LEN} characters")
    return {str(k): v for k, v in value.items()}


def _flatten_actions(actions_raw: list) -> tuple[list[dict], list | None, list[str]]:
    """Flatten ``actions`` (steps, ``if/then/else`` branches and ``use:``
    references) into leaves.

    Returns ``(leaves, flow, uses)``: the leaf step mappings in document
    order, each with a ``branch`` path (None for a top-level step, otherwise a
    dot path like ``b1.then`` / ``b1.else.b2.then``); the ``flow`` list of
    nodes (``{"step": <leaf>}`` / ``{"id", "if", "then", "else"}`` /
    ``{"use": <name>}``), or None when the task has no branch; and the sorted
    list of distinct task names referenced with ``use:``. A ``use:`` item is
    allowed only inside a ``then``/else`` list — it names a whole task whose
    steps are spliced in at deploy (phase 05b), not a step to validate now.
    Branch shape is validated here (``if`` a non-empty string, ``then`` a
    non-empty list, optional ``else`` list, no other keys); branch conditions
    are validated later, once every step type is known.
    """
    leaves: list[dict] = []
    branch_counter = 0
    uses: set[str] = set()

    def _walk(entries: list, depth: int, path: str, where: str) -> list:
        nonlocal branch_counter
        nodes: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise SpecError(f"{where} must be a mapping")
            if "use" in entry:
                if depth == 0:
                    raise SpecError(
                        "use: belongs inside a then/else branch — to reuse a "
                        "whole task at the top level, use a playbook"
                    )
                if set(entry) != {"use"}:
                    raise SpecError(
                        f"{where}: a use reference has exactly one key — use"
                    )
                name = entry["use"]
                if not isinstance(name, str) or not name.strip():
                    raise SpecError(f"{where}.use must be a non-empty string")
                if len(name) > 200:
                    raise SpecError(
                        f"{where}.use: task name too long (max 200)")
                uses.add(name.strip())
                nodes.append({"use": name.strip()})
            elif "if" in entry:
                if depth + 1 > _MAX_BRANCH_DEPTH:
                    raise SpecError(
                        f"{where}: branch nested deeper than {_MAX_BRANCH_DEPTH} levels"
                    )
                unknown = set(entry) - {"if", "then", "else"}
                if unknown:
                    raise SpecError(
                        f"{where}: branch has unknown key {min(unknown)!r} — "
                        f"a branch is if/then/else only"
                    )
                cond = entry.get("if")
                if not isinstance(cond, str) or not cond.strip():
                    raise SpecError(f"{where}.if must be a non-empty string")
                then_raw = entry.get("then")
                if not isinstance(then_raw, list) or not then_raw:
                    raise SpecError(f"{where}.then must be a non-empty list")
                else_raw = entry.get("else")
                if else_raw is not None and (
                        not isinstance(else_raw, list) or not else_raw):
                    raise SpecError(
                        f"{where}.else must be a non-empty list when present")
                branch_counter += 1
                bid = f"b{branch_counter}"
                node_where = f"branch {bid}"
                then_nodes = _walk(then_raw, depth + 1,
                                   f"{path}{bid}.then.", node_where + ".then")
                else_nodes = _walk(else_raw or [], depth + 1,
                                   f"{path}{bid}.else.", node_where + ".else")
                node = {"id": bid, "if": cond, "then": then_nodes,
                        "else": else_nodes}
                nodes.append(node)
            else:
                entry["branch"] = path.rstrip(".") or None
                nodes.append({"step": entry})
                leaves.append(entry)
        return nodes

    top = _walk(actions_raw, 0, "", "actions")
    flow = top if any("id" in node for node in top) else None
    if len(leaves) > _MAX_BRANCH_LEAVES:
        raise SpecError(
            f"too many steps across all branches (max {_MAX_BRANCH_LEAVES})"
        )
    return leaves, flow, sorted(uses)


def _flow_by_id(nodes: list, leaf_ids: dict[int, str]) -> list:
    """The flow with each leaf mapping replaced by its step id."""
    out = []
    for node in nodes:
        if "step" in node:
            out.append({"step": leaf_ids[id(node["step"])]})
        elif "use" in node:
            out.append({"use": node["use"]})
        else:
            out.append({"id": node["id"], "if": node["if"],
                        "then": _flow_by_id(node["then"], leaf_ids),
                        "else": _flow_by_id(node["else"], leaf_ids)})
    return out


def _check_status_values(tree: ast.Expression, where: str) -> None:
    """A step's status compared with a string must name a real status.

    ``steps.up.status == "success"`` validates as an expression and is false
    on every host, so the step it guards never runs — and nothing says why.
    """
    def _is_status(node):
        return (isinstance(node, ast.Attribute) and node.attr == "status"
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "steps")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        for op, (left, right) in zip(node.ops, zip(sides, sides[1:])):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            for a, b in ((left, right), (right, left)):
                if (_is_status(a) and isinstance(b, ast.Constant)
                        and isinstance(b.value, str) and b.value not in STEP_STATUSES):
                    raise SpecError(
                        f"{where}: a step's status is one of "
                        f"{', '.join(STEP_STATUSES)}, not {b.value!r}"
                    )


def _check_ordering_types(
    expr: str, tree: ast.Expression, earlier: dict[str, str], where: str
) -> None:
    """An ordering comparison may not name a str/bool step output.

    The evaluator treats ``"a" < 5`` as False (never a TypeError), so
    ``when: steps.svc.result.name > 0`` would silently be false on every
    host — the author almost certainly meant a number.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comp in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                continue
            # In a chained comparison the left side of each op is the previous
            # comparator; judge both ends of this op.
            left_side = (node.left if op is node.ops[0]
                         else node.comparators[node.ops.index(op) - 1])
            for side in (left_side, comp):
                # side is steps.<id>.result.<field>: attribute chain
                # side.attr (field) → side.value.attr ("result") →
                # side.value.value.attr (step id) → side.value.value.value
                # is the "steps" Name.
                if not (isinstance(side, ast.Attribute)
                        and isinstance(side.value, ast.Attribute)
                        and side.value.attr == "result"
                        and isinstance(side.value.value, ast.Attribute)):
                    continue
                step_id = side.value.value.attr
                if step_id not in earlier:
                    continue  # unknown id — reported by the step-ref check
                declared = action_outputs(earlier[step_id]).get(side.attr)
                if declared in ("str", "bool"):
                    raise SpecError(
                        f"{where}: steps.{step_id}.result.{side.attr} is a "
                        f"{declared} output; <, <=, >, >= compare numbers"
                    )


def stays_open_seconds(value: Any, where: str = "stays_open") -> int:
    """Normalize a hunt step's ``stays_open`` param to seconds.

    Accepts a string of the form ``<n>m``, ``<n>h`` or ``<n>d``, or an int
    number of seconds. Must land between one hour and 30 days.
    """
    if isinstance(value, bool):
        raise SpecError(f"{where} must be a duration string ('<n>m', '<n>h', '<n>d') or seconds")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        match = _STAYS_OPEN_PATTERN.match(value.strip())
        if not match:
            raise SpecError(
                f"{where} must be a duration string ('<n>m', '<n>h', '<n>d') or seconds, got {value!r}"
            )
        amount, unit = int(match.group(1)), match.group(2)
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    else:
        raise SpecError(f"{where} must be a duration string ('<n>m', '<n>h', '<n>d') or seconds")
    if seconds < _STAYS_OPEN_MIN or seconds > _STAYS_OPEN_MAX:
        raise SpecError(f"{where} must be between 1 hour and 30 days, got {seconds}s")
    return seconds


def hunt_expiry(parsed_or_steps: Any, now: datetime) -> datetime | None:
    """When a hunt task may stop waiting for a host that never checks in.

    Takes a parsed spec dict or a step list (as deployed into
    ``params["steps"]``); returns ``now + max(stays_open over hunt steps)`` —
    the default 7 days when no hunt step sets one — or ``None`` when no step
    is a hunt, which leaves ``Task.expires_at`` untouched.
    """
    key = "type" if isinstance(parsed_or_steps, dict) else "action"
    steps = (
        parsed_or_steps.get("actions") or []
        if isinstance(parsed_or_steps, dict)
        else parsed_or_steps
    )
    values: list[int] = []
    has_hunt = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        if not str(step.get(key, "")).startswith("hunt_"):
            continue
        has_hunt = True
        params = step.get("params") or {}
        where = f"step {step.get('id', '?')}.stays_open"
        if params.get("stays_open") is None:
            values.append(DEFAULT_STAYS_OPEN)
        else:
            values.append(stays_open_seconds(params["stays_open"], where))
    if not has_hunt:
        return None
    return now + timedelta(seconds=max(values))


def _check_variable_refs(
    value: Any,
    declared_ids: set[str],
    where: str,
    warnings: list[str],
    earlier: dict[str, str],
) -> None:
    """Recursively confirm every input and step reference is valid.

    Accepts both the current ${{ inputs.x }} form and the pre-2026.13
    {{ inputs.x }} form (the latter collects a deprecation warning), and
    ${{ steps.<id>.status }} / ${{ steps.<id>.result.<field> }} for steps
    listed in ``earlier`` (step id → action type).
    """
    if isinstance(value, str):
        step_refs = _STEP_REF.findall(value)
        if len(_STEPS_MARKER.findall(value)) != len(step_refs):
            raise SpecError(
                f"{where}: malformed step reference — use "
                f"${{{{ steps.<id>.status }}}} or ${{{{ steps.<id>.result.<field> }}}}"
            )
        for step_id, _status, field in step_refs:
            _check_step_ref(step_id, field or None, earlier, where)
        for match in _INPUT_REF.finditer(value):
            ref = match.group(1)
            if ref not in declared_ids:
                raise SpecError(f"{where}: unknown input reference ${{{{ inputs.{ref} }}}}")
        for match in _LEGACY_INPUT_REF.finditer(value):
            ref = match.group(1)
            if ref not in declared_ids:
                raise SpecError(f"{where}: unknown input reference {{{{ inputs.{ref} }}}}")
            warning = (
                f"{where}: {{{{ inputs.{ref} }}}} is the old input syntax — "
                f"write ${{{{ inputs.{ref} }}}}"
            )
            if warning not in warnings:
                warnings.append(warning)
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_variable_refs(v, declared_ids, where, warnings, earlier)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_variable_refs(v, declared_ids, f"{where}[{i}]", warnings, earlier)


__all__ = ["_check_ordering_types", "_check_status_values", "_check_step_ref", "_check_variable_refs", "_flatten_actions", "_flow_by_id", "_max_risk", "_validate_action_inputs", "_validate_on_failure", "_validate_schedule", "_validate_success_criteria", "hunt_expiry", "schedule_window_active", "stays_open_seconds"]
