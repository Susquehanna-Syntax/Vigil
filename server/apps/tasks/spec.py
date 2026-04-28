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
from typing import Any

import yaml


class SpecError(ValueError):
    """Raised when a YAML task definition fails validation."""


# ── Action registry ──────────────────────────────────────────────────────────
#
# Keep this list in lockstep with ``agent/vigil_agent/executor.py``. Each entry
# records required params, a risk tier, and a human label for the UI.

ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    # ── Service management ──────────────────────────────────────────────────
    "restart_service": {
        "label": "Restart service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
    },
    "start_service": {
        "label": "Start service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
    },
    "stop_service": {
        "label": "Stop service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
    },
    "reload_service": {
        "label": "Reload service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
    },
    "enable_service": {
        "label": "Enable service",
        "risk": "low",
        "required": ["service_name"],
        "optional": [],
    },
    "disable_service": {
        "label": "Disable service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
    },
    "check_service": {
        "label": "Check service status",
        "risk": "low",
        "required": ["service_name"],
        "optional": ["expect"],
    },
    # ── Container management ────────────────────────────────────────────────
    "restart_container": {
        "label": "Restart container",
        "risk": "standard",
        "required": ["container_name"],
        "optional": [],
    },
    "start_container": {
        "label": "Start container",
        "risk": "low",
        "required": ["container_name"],
        "optional": [],
    },
    "stop_container": {
        "label": "Stop container",
        "risk": "standard",
        "required": ["container_name"],
        "optional": [],
    },
    "pull_image": {
        "label": "Pull container image",
        "risk": "low",
        "required": ["image"],
        "optional": [],
    },
    "remove_container": {
        "label": "Remove container",
        "risk": "high",
        "required": ["container_name"],
        "optional": [],
    },
    "docker_compose_up": {
        "label": "Docker Compose up",
        "risk": "standard",
        "required": ["compose_file"],
        "optional": ["services"],  # comma-separated service names
    },
    "docker_compose_down": {
        "label": "Docker Compose down",
        "risk": "standard",
        "required": ["compose_file"],
        "optional": [],
    },
    "clear_docker_logs": {
        "label": "Truncate Docker logs",
        "risk": "low",
        "required": [],
        "optional": ["container_name"],
    },
    # ── File / directory operations ─────────────────────────────────────────
    "write_file": {
        "label": "Write file",
        "risk": "high",
        "required": ["path", "content"],
        "optional": ["mode"],
    },
    "create_directory": {
        "label": "Create directory",
        "risk": "low",
        "required": ["path"],
        "optional": ["owner", "group", "mode"],
    },
    "delete_path": {
        "label": "Delete path",
        "risk": "high",
        "required": ["path"],
        "optional": ["recursive"],
    },
    "copy_file": {
        "label": "Copy file",
        "risk": "standard",
        "required": ["src", "dest"],
        "optional": [],
    },
    "move_file": {
        "label": "Move file",
        "risk": "standard",
        "required": ["src", "dest"],
        "optional": [],
    },
    "set_permissions": {
        "label": "Set permissions",
        "risk": "standard",
        "required": ["path"],
        "optional": ["owner", "group", "mode"],
    },
    # ── Package management ──────────────────────────────────────────────────
    "install_package": {
        "label": "Install package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
    },
    "remove_package": {
        "label": "Remove package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
    },
    "update_package": {
        "label": "Update package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
    },
    "run_package_updates": {
        "label": "Run system updates",
        "risk": "standard",
        "required": [],
        "optional": ["security_only"],
    },
    # ── System ──────────────────────────────────────────────────────────────
    "clear_temp_files": {
        "label": "Clear /tmp",
        "risk": "low",
        "required": [],
        "optional": ["older_than_days"],
    },
    "execute_script": {
        "label": "Execute allowlisted script",
        "risk": "high",
        "required": ["script_name"],
        "optional": [],
    },
    "reboot": {
        "label": "Reboot host",
        "risk": "high",
        "required": [],
        "optional": ["delay_seconds"],
    },
    "run_command": {
        "label": "Run shell command",
        "risk": "high",
        "required": ["command"],
        "optional": ["timeout"],
    },
    "set_hostname": {
        "label": "Set hostname",
        "risk": "standard",
        "required": ["hostname"],
        "optional": [],
    },
    # ── Networking ──────────────────────────────────────────────────────────
    "add_firewall_rule": {
        "label": "Add firewall rule",
        "risk": "high",
        "required": ["port", "protocol"],
        "optional": ["action"],
    },
    "remove_firewall_rule": {
        "label": "Remove firewall rule",
        "risk": "high",
        "required": ["port", "protocol"],
        "optional": [],
    },
    # ── User management ────────────────────────────────────────────────────
    "create_user": {
        "label": "Create user",
        "risk": "high",
        "required": ["username"],
        "optional": ["groups", "shell"],  # groups: comma-separated
    },
    "delete_user": {
        "label": "Delete user",
        "risk": "high",
        "required": ["username"],
        "optional": ["remove_home"],
    },
    "add_user_to_group": {
        "label": "Add user to group",
        "risk": "standard",
        "required": ["username", "group"],
        "optional": [],
    },
    # ── Cron ────────────────────────────────────────────────────────────────
    "create_cron_job": {
        "label": "Create cron job",
        "risk": "standard",
        "required": ["schedule", "command"],
        "optional": ["user"],
    },
    "delete_cron_job": {
        "label": "Delete cron job",
        "risk": "standard",
        "required": ["pattern"],
        "optional": ["user"],
    },
}

_RISK_ORDER = {"low": 0, "standard": 1, "high": 2}
_VALID_RISK = set(_RISK_ORDER)

_INPUT_TYPES = {"text", "choice", "boolean", "number"}
# Variable references look like {{ inputs.foo }} — whitespace flexible.
_VAR_PATTERN = re.compile(r"\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_INPUT_ID_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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


def _check_variable_refs(value: Any, declared_ids: set[str], where: str) -> None:
    """Recursively confirm every {{ inputs.x }} reference matches a declared input."""
    if isinstance(value, str):
        for match in _VAR_PATTERN.finditer(value):
            ref = match.group(1)
            if ref not in declared_ids:
                raise SpecError(f"{where}: unknown input reference {{{{ inputs.{ref} }}}}")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_variable_refs(v, declared_ids, where)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_variable_refs(v, declared_ids, f"{where}[{i}]")


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
                return str(resolved[m.group(1)])
            return _VAR_PATTERN.sub(repl, value)
        if isinstance(value, dict):
            return {k: _sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sub(v) for v in value]
        return value

    new_actions = []
    for action in parsed_spec.get("actions", []):
        new_actions.append({**action, "params": _sub(action.get("params") or {})})

    return {**parsed_spec, "actions": new_actions, "resolved_inputs": resolved}


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

    for index, entry in enumerate(actions_raw):
        if not isinstance(entry, dict):
            raise SpecError(f"action #{index + 1} must be a mapping")

        action_type = _as_str(entry.get("type"), f"actions[{index}].type", max_len=64)
        if not action_type:
            raise SpecError(f"action #{index + 1} missing 'type'")
        if action_type not in ACTION_REGISTRY:
            raise SpecError(
                f"action #{index + 1}: unknown type {action_type!r} — "
                f"must be one of {', '.join(sorted(ACTION_REGISTRY))}"
            )

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
            # If the value references {{ inputs.x }}, the input must exist.
            _check_variable_refs(pv, declared_input_ids, f"action #{index + 1} param {pk!r}")

        parsed_actions.append({
            "id": action_id,
            "type": action_type,
            "label": spec["label"],
            "params": params,
            "risk": spec["risk"],
        })

        derived_risk_level = max(derived_risk_level, _RISK_ORDER[spec["risk"]])

    # Effective risk is max(declared risk, derived from actions) — users
    # cannot declare a lower risk than the actions actually warrant.
    effective_risk_level = max(_RISK_ORDER[risk], derived_risk_level)
    effective_risk = next(k for k, v in _RISK_ORDER.items() if v == effective_risk_level)

    return {
        "name": name,
        "description": description,
        "relevance": relevance,
        "risk": effective_risk,
        "declared_risk": risk,
        "actions": parsed_actions,
        "inputs": declared_inputs,
    }
