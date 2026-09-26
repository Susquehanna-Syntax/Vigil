"""YAML parsing and validation for TaskDefinition specs.

A task definition looks like::

    name: Restart nginx and verify
    description: Bounce nginx then hit the health endpoint
    relevance: web servers
    risk: standard
    actions:
      - id: bounce
        type: restart_service
        params: { service_name: nginx }
      - id: verify
        type: execute_script
        params: { script_name: healthcheck.sh }

The action ``type`` must be in :data:`ACTION_REGISTRY`, which mirrors the
agent-side executor. The server never emits raw commands; it only names
actions that the agent already knows how to run.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

import yaml

# The action registry lives in registry.py; re-exported for existing importers.
from .registry import (  # noqa: F401
    ACTION_REGISTRY,
    LEGACY_ACTION_ALIASES,
    action_outputs,
)
from .scripthash import script_hash


class SpecError(ValueError):
    """Raised when a YAML task definition fails validation."""


_RISK_ORDER = {"low": 0, "standard": 1, "high": 2}

#: Ceiling for a per-step ``timeout:``, in seconds. Mirrors the agent's own
#: validation in executor._run_command — the two must agree, or the server
#: accepts a value the agent then rejects.
_MAX_STEP_TIMEOUT = 3600
_VALID_RISK = set(_RISK_ORDER)

#: How long a hunt stays open for hosts that never check in, in seconds —
#: used when a hunt step does not set its own ``stays_open``.
DEFAULT_STAYS_OPEN = 7 * 86400
_STAYS_OPEN_MIN = 3600
_STAYS_OPEN_MAX = 30 * 86400
_STAYS_OPEN_PATTERN = re.compile(r"^(\d+)([mhd])$")

_INPUT_TYPES = {"text", "choice", "boolean", "number"}
# Input references: ${{ inputs.foo }} (whitespace flexible). The ${{ }} marker is not valid
# bash, PowerShell or YAML, so script text can never be mistaken for an input. $${{ is an
# escaped literal ${{ — never a marker (the agent turns it back into ${{).
_INPUT_REF = re.compile(r"(?<!\$)\$\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# The pre-2026.13 form, {{ inputs.foo }} — accepted with a warning for one release.
_LEGACY_INPUT_REF = re.compile(r"(?<!\$)\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Step references: ${{ steps.<id>.status }} or ${{ steps.<id>.result.<field> }}.
# Either form, in one pattern, for single-pass substitution.
_ANY_INPUT_REF = re.compile(r"(?<!\$)(\$)?\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_STEPS_MARKER = re.compile(r"(?<!\$)\$\{\{\s*steps\.")
_STEP_REF = re.compile(
    r"(?<!\$)\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.(?:(status)|result\.([A-Za-z_][A-Za-z0-9_]*))\s*\}\}"
)
_INPUT_ID_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Metadata fields (optional, community-repo oriented). A task may name the
# CVEs it remediates, link the advisories it came from, and declare which
# OS families it applies to. None of them affect execution or risk — they
# exist so the Community tab and the vuln views can cross-reference.
_CVE_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)
_VALID_PLATFORMS = {"linux", "windows", "darwin"}

# Day-of-week aliases accepted in `schedule.window.days`. Stored canonically
# as 0..6 with 0 = Monday (matches Python's datetime.weekday()).
_DAY_ALIASES: dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_ALL_DAYS = list(range(7))


def _as_str(value: Any, field: str, max_len: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SpecError(f"{field!r} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise SpecError(f"{field!r} is too long (max {max_len})")
    return value


def _validate_inputs(raw_inputs: Any) -> list[dict[str, Any]]:
    """Validate top-level ``inputs:`` schema. Returns canonical list."""
    if raw_inputs is None:
        return []
    if not isinstance(raw_inputs, list):
        raise SpecError("'inputs' must be a list")
    if len(raw_inputs) > 16:
        raise SpecError("too many inputs (max 16)")

    seen_ids: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_inputs):
        if not isinstance(entry, dict):
            raise SpecError(f"input #{index + 1} must be a mapping")

        input_id = _as_str(entry.get("id"), f"inputs[{index}].id", max_len=60)
        if not input_id:
            raise SpecError(f"input #{index + 1} missing 'id'")
        if not _INPUT_ID_PATTERN.match(input_id):
            raise SpecError(f"input id {input_id!r} must match [A-Za-z_][A-Za-z0-9_]*")
        if input_id in seen_ids:
            raise SpecError(f"duplicate input id {input_id!r}")
        seen_ids.add(input_id)

        input_type = _as_str(entry.get("type") or "text", f"inputs[{index}].type", max_len=16).lower()
        if input_type not in _INPUT_TYPES:
            raise SpecError(
                f"input {input_id!r}: unknown type {input_type!r} — "
                f"must be one of {', '.join(sorted(_INPUT_TYPES))}"
            )

        label = _as_str(entry.get("label") or input_id, f"inputs[{index}].label", max_len=120)
        description = _as_str(entry.get("description"), f"inputs[{index}].description", max_len=255)

        choices: list[dict[str, str]] = []
        if input_type == "choice":
            raw_choices = entry.get("choices") or []
            if not isinstance(raw_choices, list) or not raw_choices:
                raise SpecError(f"input {input_id!r}: 'choices' must be a non-empty list")
            if len(raw_choices) > 32:
                raise SpecError(f"input {input_id!r}: too many choices (max 32)")
            for ci, choice in enumerate(raw_choices):
                if not isinstance(choice, dict):
                    raise SpecError(f"input {input_id!r}: choice #{ci + 1} must be a mapping")
                cv = _as_str(choice.get("value"), f"inputs[{index}].choices[{ci}].value", max_len=120)
                if not cv:
                    raise SpecError(f"input {input_id!r}: choice #{ci + 1} missing 'value'")
                cl = _as_str(choice.get("label") or cv, f"inputs[{index}].choices[{ci}].label", max_len=120)
                choices.append({"value": cv, "label": cl})

        default = entry.get("default")
        if input_type == "boolean":
            default = bool(default) if default is not None else False
        elif input_type == "number":
            if default is None:
                default = 0
            elif not isinstance(default, (int, float)) or isinstance(default, bool):
                raise SpecError(f"input {input_id!r}: 'default' must be a number")
        elif input_type == "choice":
            valid_values = {c["value"] for c in choices}
            if default is None:
                default = choices[0]["value"]
            else:
                default = _as_str(default, f"inputs[{index}].default", max_len=120)
                if default not in valid_values:
                    raise SpecError(f"input {input_id!r}: default {default!r} not in choices")
        else:  # text
            default = _as_str(default, f"inputs[{index}].default", max_len=500) if default is not None else ""

        canonical.append({
            "id": input_id,
            "type": input_type,
            "label": label,
            "description": description,
            "choices": choices,
            "default": default,
            "required": bool(entry.get("required", input_type != "boolean")),
        })

    return canonical


def _validate_tag_param(raw: Any, position: int, action_type: str) -> None:
    """Check the ``tags`` param of an add_tag/remove_tag action.

    Comma-separated, not a list: every param value has to be a primitive so
    the signed payload stays flat (see the primitive check in
    parse_and_validate).

    A value containing ``{{ inputs.x }}`` cannot be judged here — it is
    resolved per deploy. The server re-checks the reserved prefix when it
    actually applies the tags, which is what covers that case.
    """
    if not isinstance(raw, str):
        raise SpecError(
            f"action #{position} ({action_type}): 'tags' must be a "
            f"comma-separated string, not {type(raw).__name__}")
    names = [t.strip() for t in raw.split(",") if t.strip()]
    if not names:
        raise SpecError(
            f"action #{position} ({action_type}): 'tags' names no tags")
    for name in names:
        if name.startswith("agent:"):
            raise SpecError(
                f"action #{position} ({action_type}): the 'agent:' tag "
                f"namespace is reserved for tags an agent advertises about "
                f"itself")
        if len(name) > 40:
            raise SpecError(
                f"action #{position} ({action_type}): tag {name!r} is longer "
                f"than 40 characters")


_SCRIPT_SHELLS = ("bash", "sh", "powershell", "pwsh")
_SCRIPT_MAX_LEN = 65536


def _validate_script_params(params: dict[str, Any], position: int) -> None:
    has_name = "script_name" in params
    has_body = "script" in params
    if has_name and has_body:
        raise SpecError(
            f"action #{position} (execute_script): give script_name or script, not both"
        )
    if not has_name and not has_body:
        raise SpecError(
            f"action #{position} (execute_script): needs script_name or script"
        )
    if "shell" in params and not has_body:
        raise SpecError(
            f"action #{position} (execute_script): shell is only used with script"
        )
    if has_body:
        shell = params.get("shell")
        if shell not in _SCRIPT_SHELLS:
            raise SpecError(
                f"action #{position} (execute_script): shell must be one of "
                f"{', '.join(_SCRIPT_SHELLS)}"
            )
        body = params["script"]
        if not isinstance(body, str) or not body:
            raise SpecError(
                f"action #{position} (execute_script): script must be a "
                f"non-empty string"
            )
        if len(body) > _SCRIPT_MAX_LEN:
            raise SpecError(
                f"action #{position} (execute_script): script is limited to "
                f"{_SCRIPT_MAX_LEN} characters"
            )
        if "${{" in body:
            raise SpecError(
                f"action #{position} (execute_script): a script body takes "
                f"inputs as $VIGIL_INPUT_<ID> environment variables, not "
                f"${{{{ … }}}} markers"
            )


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

    Used by the dispatch path (hosts/views.py) to decide whether a pending
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


def resolve_inputs(parsed_spec: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Substitute supplied input values into action params.

    Returns a copy of ``parsed_spec`` with ``actions[*].params`` rewritten so
    that every ``{{ inputs.x }}`` placeholder is replaced with the resolved
    value. Raises :class:`SpecError` if a required input is missing or a value
    fails type/choice validation.
    """
    declared = parsed_spec.get("inputs") or []
    if not declared:
        return parsed_spec

    resolved: dict[str, Any] = {}
    for inp in declared:
        iid = inp["id"]
        supplied = values.get(iid, inp["default"])
        itype = inp["type"]

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
            valid = {c["value"] for c in inp["choices"]}
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

    return {**parsed_spec, "actions": new_actions, "success_criteria": new_sc, "resolved_inputs": resolved}


def _validate_cves(value: Any) -> list[str]:
    """Optional ``cves`` list — at most 32 unique, normalized CVE ids."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'cves' must be a list")
    if len(value) > 32:
        raise SpecError("'cves' may hold at most 32 entries")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        cve = str(item).strip().upper()
        if not _CVE_PATTERN.match(cve):
            raise SpecError(f"'cves' entry '{cve}' is not a valid CVE id")
        if cve in seen:
            raise SpecError(f"'cves' entry '{cve}' is duplicated")
        seen.add(cve)
        out.append(cve)
    return out


def _validate_references(value: Any) -> list[str]:
    """Optional ``references`` list — absolute http(s) URLs only."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'references' must be a list")
    if len(value) > 16:
        raise SpecError("'references' may hold at most 16 entries")
    out: list[str] = []
    for item in value:
        ref = str(item).strip()
        if len(ref) > 500:
            raise SpecError("'references' entries must be at most 500 characters")
        parsed = urllib.parse.urlparse(ref)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SpecError(
                f"'references' entry '{ref}' must be an absolute http(s) URL"
            )
        out.append(ref)
    return out


def _validate_platforms(value: Any) -> list[str]:
    """Optional ``platforms`` list — a subset of the known OS families."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'platforms' must be a list")
    if len(value) > 3:
        raise SpecError("'platforms' may hold at most 3 entries")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        platform = str(item).strip().lower()
        if platform not in _VALID_PLATFORMS:
            raise SpecError(
                f"'platforms' entry '{platform}' is not a known platform "
                f"({' or '.join(sorted(_VALID_PLATFORMS))})"
            )
        if platform in seen:
            raise SpecError(f"'platforms' entry '{platform}' is duplicated")
        seen.add(platform)
        out.append(platform)
    return out


def parse_and_validate(yaml_source: str) -> dict[str, Any]:
    """Parse YAML, validate structure, return a canonical ``parsed_spec`` dict.

    The returned dict is stable: it always contains ``name``, ``description``,
    ``relevance``, ``risk``, ``actions`` (list of ``{id, type, params}``), and
    ``derived_risk`` (the max risk across all actions). The caller should
    store this alongside the raw YAML source.
    """
    if not yaml_source or not yaml_source.strip():
        raise SpecError("YAML is empty")

    try:
        raw = yaml.safe_load(yaml_source)
    except yaml.YAMLError as exc:
        raise SpecError(f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SpecError("Top-level YAML must be a mapping")

    name = _as_str(raw.get("name"), "name", max_len=120)
    if not name:
        raise SpecError("'name' is required")

    description = _as_str(raw.get("description"), "description", max_len=2000)
    relevance = _as_str(raw.get("relevance"), "relevance", max_len=255)

    # ``author`` and ``created`` are optional locally but auto-injected by the
    # "Submit to Community" flow so every YAML that lands on the community
    # repo is self-describing (who wrote it, when). When present, ``created``
    # must be an ISO-8601 calendar date (YYYY-MM-DD); ``author`` is any short
    # string. Both flow through to ``parsed_spec`` so the UI can render them.
    author = _as_str(raw.get("author"), "author", max_len=80)
    # PyYAML auto-parses ISO dates into ``datetime.date`` objects; accept
    # both forms so authors can write either ``created: 2026-05-15`` (parsed
    # as a date) or ``created: "2026-05-15"`` (parsed as a string).
    raw_created = raw.get("created")
    if hasattr(raw_created, "isoformat") and not isinstance(raw_created, str):
        raw_created = raw_created.isoformat()
    created = _as_str(raw_created, "created", max_len=10)
    if created and not _ISO_DATE_PATTERN.match(created):
        raise SpecError("'created' must be an ISO-8601 date (YYYY-MM-DD)")

    # ``uid`` is this task's identity in the community catalog, independent of
    # its name and its filename. A playbook references its steps by uid, which
    # is what lets the reference survive a rename and stops two tasks that
    # happen to share a name from being confused for each other. Optional:
    # everything written before uids existed has none, and a task that is never
    # shared never needs one.
    from vigil.contentyaml import ContentYamlError, parse_uid

    try:
        uid = parse_uid(raw, "task")
    except ContentYamlError as exc:
        raise SpecError(str(exc)) from exc

    # Community-oriented metadata. None of it affects execution or risk; it
    # exists so the Community tab and the vuln views can cross-reference a
    # task with the advisories and platforms it targets.
    cves = _validate_cves(raw.get("cves"))
    references = _validate_references(raw.get("references"))
    platforms = _validate_platforms(raw.get("platforms"))

    risk = _as_str(raw.get("risk") or "standard", "risk", max_len=16).lower()
    if risk not in _VALID_RISK:
        raise SpecError(f"'risk' must be one of: {', '.join(sorted(_VALID_RISK))}")

    declared_inputs = _validate_inputs(raw.get("inputs"))
    declared_input_ids = {inp["id"] for inp in declared_inputs}

    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        raise SpecError("'actions' must be a non-empty list")
    if len(actions_raw) > 32:
        raise SpecError("too many actions (max 32)")

    parsed_actions: list[dict[str, Any]] = []
    derived_risk_level = 0
    seen_ids: set[str] = set()
    warnings: list[str] = []

    for index, entry in enumerate(actions_raw):
        if not isinstance(entry, dict):
            raise SpecError(f"action #{index + 1} must be a mapping")

        action_type = _as_str(entry.get("type"), f"actions[{index}].type", max_len=64)
        if not action_type:
            raise SpecError(f"action #{index + 1} missing 'type'")
        action_type = LEGACY_ACTION_ALIASES.get(action_type, action_type)
        entry["type"] = action_type
        if action_type not in ACTION_REGISTRY:
            raise SpecError(
                f"action #{index + 1}: unknown type {action_type!r} — "
                f"must be one of {', '.join(sorted(ACTION_REGISTRY))}"
            )

        # Steps before this one — the only ones a reference may name.
        earlier = {a["id"]: a["type"] for a in parsed_actions}

        action_id = _as_str(entry.get("id") or f"step{index + 1}", f"actions[{index}].id", max_len=60)
        if action_id in seen_ids:
            raise SpecError(f"duplicate action id {action_id!r}")
        seen_ids.add(action_id)

        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise SpecError(f"action #{index + 1}: 'params' must be a mapping")

        spec = ACTION_REGISTRY[action_type]
        for required in spec["required"]:
            if required not in params:
                raise SpecError(
                    f"action #{index + 1} ({action_type}) missing required param {required!r}"
                )

        if action_type in ("add_tag", "remove_tag"):
            _validate_tag_param(params.get("tags", ""), index + 1, action_type)
        elif action_type == "execute_script":
            _validate_script_params(params, index + 1)
        elif action_type == "hunt_file" and not (
                params.get("name") or params.get("sha256")):
            raise SpecError(
                f"action #{index + 1} (hunt_file): needs name or sha256 — "
                "it cannot search without at least one of the two"
            )
        elif action_type == "hunt_process" and not (
                params.get("name") or params.get("cmdline") or params.get("user")):
            raise SpecError(
                f"action #{index + 1} (hunt_process): needs name, cmdline or user — "
                "it cannot search without at least one of the three"
            )
        elif action_type == "hunt_port" and not (
                params.get("port") or params.get("process")):
            raise SpecError(
                f"action #{index + 1} (hunt_port): needs port or process — "
                "it cannot search without at least one of the two"
            )
        elif action_type == "hunt_content":
            _return_raw = params.get("return")
            if _return_raw is not None and str(_return_raw).lower() not in (
                    "match", "lines", "text"):
                raise SpecError(
                    f"action #{index + 1} (hunt_content): return must be one "
                    f"of match, lines or text, got {str(_return_raw)!r}"
                )
            try:
                re.compile(str(params["pattern"]))
            except re.error as exc:
                raise SpecError(
                    f"action #{index + 1} (hunt_content): invalid pattern — "
                    f"{exc}"
                ) from exc

        allowed = set(spec["required"]) | set(spec["optional"])
        extra = set(params) - allowed
        if extra:
            raise SpecError(
                f"action #{index + 1} ({action_type}) has unknown params: {sorted(extra)}"
            )

        # All param values must be primitives for safe signing
        for pk, pv in params.items():
            if not isinstance(pv, (str, int, float, bool)):
                raise SpecError(
                    f"action #{index + 1}: param {pk!r} must be a primitive value"
                )
            # If the value references ${{ inputs.x }}, the input must exist.
            # The inline script body is an exception: bare braces are literal
            # shell/PowerShell text, so its contents are never ref-checked.
            if not (action_type == "execute_script" and pk == "script"):
                _check_variable_refs(
                    pv, declared_input_ids, f"action #{index + 1} param {pk!r}",
                    warnings, earlier,
                )

        # Optional `when:` predicate. Validated for syntactic safety
        # here; the agent evaluates it at runtime against its own
        # platform context. Bad syntax fails the save, not the deploy.
        when_raw = entry.get("when")
        when_expr = ""
        if when_raw is not None:
            when_expr = _as_str(when_raw, f"actions[{index}].when", max_len=500)
            if when_expr:
                from .expression import (
                    ExprError,
                    referenced_inputs as _refs,
                    referenced_steps as _step_refs,
                    validate as _validate_when,
                )
                try:
                    _validate_when(when_expr)
                except ExprError as exc:
                    raise SpecError(
                        f"action #{index + 1}: when expression rejected: {exc}"
                    ) from exc
                # An inputs.* reference that was never declared evaluates to
                # None at runtime, and None == "yes" is simply false — so the
                # step would skip silently on every run, forever. Fail the
                # save instead.
                unknown = _refs(when_expr) - declared_input_ids
                if unknown:
                    raise SpecError(
                        f"action #{index + 1}: when expression references "
                        f"undeclared input(s) {sorted(unknown)} — declare them "
                        f"under inputs:, or the step will silently never run"
                    )
                try:
                    step_refs = _step_refs(when_expr)
                except ExprError as exc:
                    raise SpecError(
                        f"action #{index + 1}: when expression rejected: {exc}"
                    ) from exc
                for step_id, field in sorted(step_refs, key=str):
                    _check_step_ref(step_id, field, earlier, f"action #{index + 1} when")

        # Optional per-step timeout, in seconds. The 1..3600 range mirrors
        # the agent's own validation — a limit the server accepts but the
        # agent refuses is just a confusing failure one hop later.
        timeout_raw = entry.get("timeout")
        step_timeout = None
        if timeout_raw is not None:
            if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
                raise SpecError(
                    f"action #{index + 1}: timeout must be a whole number of "
                    f"seconds")
            if not 1 <= timeout_raw <= _MAX_STEP_TIMEOUT:
                raise SpecError(
                    f"action #{index + 1}: timeout must be between 1 and "
                    f"{_MAX_STEP_TIMEOUT} seconds")
            step_timeout = timeout_raw

        # stays_open is a hunt-only param; it never reaches the agent.
        if action_type.startswith("hunt_") and params.get("stays_open") is not None:
            stays_open_seconds(
                params["stays_open"], f"action #{index + 1} ({action_type}) stays_open"
            )

        parsed_actions.append({
            "id": action_id,
            "type": action_type,
            "label": spec["label"],
            "params": params,
            "risk": spec["risk"],
            "when": when_expr,
            "timeout": step_timeout,
            "outputs": sorted(action_outputs(action_type)),
        })
        if action_type == "execute_script" and "script" in params:
            parsed_actions[-1]["script_sha256"] = script_hash(params["script"])

        derived_risk_level = max(derived_risk_level, _RISK_ORDER[spec["risk"]])

        # hunt_content with `return: text` carries matched text out of the
        # host, so the step itself is high risk regardless of what the rest
        # of the task declares.
        if action_type == "hunt_content" and (
                str(params.get("return") or "lines").lower() == "text"):
            derived_risk_level = max(derived_risk_level, _RISK_ORDER["high"])

    # Effective risk is max(declared risk, derived from actions) — users
    # cannot declare a lower risk than the actions actually warrant.
    effective_risk_level = max(_RISK_ORDER[risk], derived_risk_level)
    effective_risk = next(k for k, v in _RISK_ORDER.items() if v == effective_risk_level)

    schedule = _validate_schedule(raw.get("schedule"))
    on_failure = _validate_on_failure(raw.get("on_failure"))
    success_criteria = _validate_success_criteria(raw.get("success_criteria"))

    # Optional `collect:` directive — turns a task into an inventory data
    # collector. Validated lightly here; the result handler reads it.
    raw_collect = raw.get("collect")
    collect: dict[str, Any] | None = None
    if raw_collect is not None:
        if not isinstance(raw_collect, dict):
            raise SpecError("'collect' must be a mapping")
        column = _as_str(raw_collect.get("column"), "collect.column", max_len=80)
        if not column:
            raise SpecError("collect.column is required")
        parse_mode = _as_str(
            raw_collect.get("parse") or "output_line_1", "collect.parse", max_len=40
        ).lower()
        if parse_mode not in {"output_line_1", "output_full", "output_trim"}:
            raise SpecError(
                "collect.parse must be one of: output_line_1, output_full, output_trim"
            )
        collect = {"column": column, "parse": parse_mode}

    target_tags = _validate_target_tags(raw.get("target_tags"))

    return {
        "name": name,
        "description": description,
        "relevance": relevance,
        "uid": uid,
        "author": author,
        "created": created,
        "cves": cves,
        "references": references,
        "platforms": platforms,
        "risk": effective_risk,
        "declared_risk": risk,
        "actions": parsed_actions,
        "inputs": declared_inputs,
        "schedule": schedule,
        "on_failure": on_failure,
        "success_criteria": success_criteria,
        "collect": collect,
        "target_tags": target_tags,
        "warnings": warnings,
    }


def _validate_target_tags(raw: Any) -> list[str]:
    """Validate the optional top-level ``target_tags:`` list.

    Schema::

        target_tags:
          - os:linux         # auto-tag namespace
          - pkg:apt
          - prod             # plain user tag — also accepted

    The deploy endpoint enforces this as an OR filter — a host is
    eligible if it carries any one of these tags (in its merged
    ``Host.tags`` list). Returns an empty list when no filter is set.
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise SpecError("'target_tags' must be a list of strings")
    if len(raw) > 32:
        raise SpecError("too many target_tags (max 32)")
    canonical: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(raw):
        s = _as_str(tag, f"target_tags[{index}]", max_len=64).lower()
        if not s:
            raise SpecError(f"target_tags[{index}] is empty")
        if s in seen:
            continue  # dedupe silently
        seen.add(s)
        canonical.append(s)
    return canonical


def validate_params_override(spec: dict, override) -> str | None:
    """Validate a per-use params override against a parsed task spec.

    Playbook steps and automations may override action params without editing
    the shared definition. ``override`` maps stringified action indexes to
    ``{param: value}`` dicts; params must be declared (required or optional)
    by that action's registry entry and values must be scalars. Returns an
    error string, or None when valid.
    """
    if not override:
        return None
    if not isinstance(override, dict):
        return "params_override must be an object"
    actions = spec.get("actions") or []
    for key, params in override.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            return f"no action at index {key!r}"
        if not 0 <= index < len(actions):
            return f"no action at index {key}"
        if not isinstance(params, dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in params.values()
        ):
            return f"params for action {index} must be a mapping of name to value"
        action_type = actions[index].get("type", "")
        entry = ACTION_REGISTRY.get(action_type)
        if entry is None:
            # Spec validation at definition save is the authority on action
            # types; don't second-guess it here.
            continue
        declared = set(entry["required"]) | set(entry["optional"])
        for name in params:
            if name not in declared:
                return f"{name!r} is not a parameter of {action_type}"
    return None
