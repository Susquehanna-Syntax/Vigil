"""Multi-step task runtime for Vigil agent.

Drives execution of multi-step task payloads that contain action steps,
if/else conditionals, and for_each loops.  Variable resolution and condition
evaluation are implemented without Python eval() — all comparisons are
explicit and operate on typed values extracted from the resolution context.

Step schema (all fields except ``type`` and ``action`` are optional):

  # Plain action step
  - name: "restart nginx"
    type: action            # default when ``type`` is absent
    action: "service.restart"
    params:
      service_name: "nginx"
    store_output: result    # stores StepResult into ctx["result"]
    timeout: 60             # override default executor timeout (seconds)

  # Conditional step
  - name: "check exit code"
    type: if
    condition: "prev.exit_code == 0"
    then:
      - type: action
        action: "system.log"
        params: {message: "success"}
    else:
      - type: action
        action: "system.log"
        params: {message: "failed"}

  # Loop step
  - name: "install packages"
    type: for_each
    list: ["curl", "htop", "vim"]   # or "{{packages}}" to resolve from context
    variable: "pkg"                 # name of the loop variable in ctx
    steps:
      - type: action
        action: "pkg.install"
        params:
          package_name: "{{pkg}}"

Variable syntax:
  ``{{var}}``         — looks up ctx["var"]
  ``{{var.attr}}``    — looks up ctx["var"]["attr"] or ctx["var"].attr
  ``{{prev.output}}`` — shortcut: ctx["prev"]["output"]

Condition syntax (evaluated left-to-right, single expression):
  ``expr OP value``
  Supported OPs: == != < > <= >=  contains  starts_with  ends_with
  Bare ``expr``  — truthy check (non-empty string, non-zero int, True)
  ``not expr``   — falsy check

All comparisons are type-aware: numeric strings are compared as numbers when
both sides parse as numbers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("vigil.runtime")

# ── StepResult ────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    """Outcome of a single executed step."""

    name: str
    action: str  # e.g. "service.restart" or "<if>" or "<for_each>"
    state: str   # "ok" | "error" | "skipped"
    output: str = ""
    exit_code: int = 0
    error: str = ""

    def to_context(self) -> dict[str, Any]:
        """Return a dict suitable for use as ``ctx["prev"]`` or a named store."""
        return {
            "name": self.name,
            "action": self.action,
            "state": self.state,
            "output": self.output,
            "exit_code": self.exit_code,
            "error": self.error,
        }


# ── Value resolution ──────────────────────────────────────────────────────────

_TEMPLATE_RE = re.compile(r"\{\{([^}]+)\}\}")


def _lookup(path: str, ctx: dict[str, Any]) -> Any:
    """Resolve a dotted path like ``prev.exit_code`` against *ctx*."""
    parts = path.strip().split(".")
    value: Any = ctx
    for part in parts:
        if isinstance(value, dict):
            if part not in value:
                return ""
            value = value[part]
        elif hasattr(value, part):
            value = getattr(value, part)
        else:
            return ""
    return value


def resolve_value(template: Any, ctx: dict[str, Any]) -> Any:
    """Substitute ``{{...}}`` placeholders in *template* using *ctx*.

    - If *template* is not a string, it is returned as-is.
    - If the template is exactly ``{{var}}`` with no surrounding text, the
      raw value (possibly a list, int, etc.) is returned unchanged so that
      ``for_each`` can iterate over non-string types.
    - Otherwise every placeholder is replaced with its string representation.
    """
    if not isinstance(template, str):
        return template

    # Exact match — preserve type
    m = _TEMPLATE_RE.fullmatch(template.strip())
    if m:
        return _lookup(m.group(1), ctx)

    # Partial substitution — stringify each placeholder
    def _sub(match: re.Match) -> str:
        val = _lookup(match.group(1), ctx)
        return "" if val is None else str(val)

    return _TEMPLATE_RE.sub(_sub, template)


def resolve_params(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve all values in a params dict."""
    out: dict[str, Any] = {}
    for key, val in params.items():
        if isinstance(val, dict):
            out[key] = resolve_params(val, ctx)
        elif isinstance(val, list):
            out[key] = [resolve_value(item, ctx) for item in val]
        else:
            out[key] = resolve_value(val, ctx)
    return out


# ── Condition evaluation ──────────────────────────────────────────────────────

_OP_RE = re.compile(
    r"^(.+?)\s+(==|!=|<=|>=|<|>|contains|starts_with|ends_with)\s+(.+)$"
)


def _coerce(a: Any, b_str: str) -> tuple[Any, Any]:
    """Try to compare as numbers; fall back to strings."""
    a_str = str(a) if not isinstance(a, str) else a
    # Remove surrounding quotes from b_str literal
    b_stripped = b_str.strip("\"'")
    try:
        return float(a_str), float(b_stripped)
    except (ValueError, TypeError):
        return a_str, b_stripped


def eval_condition(expr: str, ctx: dict[str, Any]) -> bool:
    """Evaluate a condition expression against *ctx*.

    Supports:
      ``lhs OP rhs``  — binary comparison
      ``not lhs``     — falsy test
      ``lhs``         — truthy test
    """
    expr = expr.strip()

    # ``not expr``
    if expr.lower().startswith("not "):
        inner = expr[4:].strip()
        return not _truthy(_lookup(inner, ctx) if "." in inner or inner in ctx else inner)

    m = _OP_RE.match(expr)
    if m:
        lhs_path, op, rhs_literal = m.group(1).strip(), m.group(2), m.group(3).strip()
        lhs = _lookup(lhs_path, ctx) if "." in lhs_path or lhs_path in ctx else lhs_path
        rhs_clean = rhs_literal.strip("\"'")

        if op == "==":
            a, b = _coerce(lhs, rhs_literal)
            return a == b
        if op == "!=":
            a, b = _coerce(lhs, rhs_literal)
            return a != b
        if op == "<":
            a, b = _coerce(lhs, rhs_literal)
            return a < b
        if op == ">":
            a, b = _coerce(lhs, rhs_literal)
            return a > b
        if op == "<=":
            a, b = _coerce(lhs, rhs_literal)
            return a <= b
        if op == ">=":
            a, b = _coerce(lhs, rhs_literal)
            return a >= b
        if op == "contains":
            return rhs_clean in str(lhs)
        if op == "starts_with":
            return str(lhs).startswith(rhs_clean)
        if op == "ends_with":
            return str(lhs).endswith(rhs_clean)

    # Bare truthy check
    val = _lookup(expr, ctx) if "." in expr or expr in ctx else expr
    return _truthy(val)


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.lower() not in ("", "0", "false", "no")
    if isinstance(val, (list, dict)):
        return bool(val)
    return bool(val)


# ── TaskRuntime ───────────────────────────────────────────────────────────────


class TaskRuntime:
    """Execute a multi-step task payload.

    Parameters
    ----------
    task_payload:
        Deserialized task dict as received from the server.  Must contain a
        ``steps`` key (list of step dicts).  May contain a ``variables`` key
        with pre-resolved variable values.
    config:
        ``AgentConfig`` instance passed through to the executor.
    on_step_result:
        Optional async-compatible callback invoked after each top-level step
        completes.  Signature: ``callback(result: StepResult) -> None``.
    """

    def __init__(
        self,
        task_payload: dict[str, Any],
        config: Any,
        on_step_result: Callable[[StepResult], None] | None = None,
    ) -> None:
        self._payload = task_payload
        self._config = config
        self._on_step_result = on_step_result
        self._results: list[StepResult] = []

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> list[StepResult]:
        """Execute all steps and return the list of StepResults."""
        steps = self._payload.get("steps", [])
        variables = self._payload.get("variables", {})

        # Seed the execution context with template variables
        ctx: dict[str, Any] = dict(variables)
        ctx["prev"] = StepResult(
            name="__init__", action="__init__", state="ok"
        ).to_context()

        self._execute_steps(steps, ctx, top_level=True)
        return self._results

    # ── Step dispatch ─────────────────────────────────────────────────────────

    def _execute_steps(
        self,
        steps: list[dict],
        ctx: dict[str, Any],
        top_level: bool = False,
    ) -> None:
        for step in steps:
            step_type = step.get("type", "action")
            name = step.get("name", step_type)

            if step_type == "action" or step_type not in ("if", "for_each"):
                result = self._execute_action(step, ctx)
            elif step_type == "if":
                result = self._execute_if(step, ctx)
            elif step_type == "for_each":
                result = self._execute_for_each(step, ctx)
            else:
                result = StepResult(
                    name=name,
                    action=step_type,
                    state="error",
                    error=f"Unknown step type: {step_type!r}",
                )

            # Update ``prev`` shortcut
            ctx["prev"] = result.to_context()

            # Store named output if requested
            store_as = step.get("store_output")
            if store_as and isinstance(store_as, str):
                ctx[store_as] = result.to_context()

            if top_level:
                self._results.append(result)
                if self._on_step_result:
                    try:
                        self._on_step_result(result)
                    except Exception as exc:
                        logger.warning("on_step_result callback raised: %s", exc)

    # ── Action step ───────────────────────────────────────────────────────────

    def _execute_action(self, step: dict, ctx: dict[str, Any]) -> StepResult:
        from .executor import execute_action  # local import avoids circular deps

        name = step.get("name", step.get("action", "action"))
        action = resolve_value(step.get("action", ""), ctx)
        raw_params = step.get("params", {}) or {}
        params = resolve_params(raw_params, ctx)
        timeout = step.get("timeout")

        logger.debug("step[action] name=%r action=%r params=%r", name, action, params)

        try:
            output = execute_action(action, params, self._config, timeout=timeout)
            return StepResult(
                name=name,
                action=str(action),
                state="ok",
                output=output or "",
                exit_code=0,
            )
        except Exception as exc:
            logger.error("step[action] %r failed: %s", action, exc)
            return StepResult(
                name=name,
                action=str(action),
                state="error",
                output="",
                exit_code=1,
                error=str(exc),
            )

    # ── If/else step ──────────────────────────────────────────────────────────

    def _execute_if(self, step: dict, ctx: dict[str, Any]) -> StepResult:
        name = step.get("name", "<if>")
        condition_expr = resolve_value(step.get("condition", "false"), ctx)
        condition_expr = str(condition_expr)

        logger.debug("step[if] name=%r condition=%r", name, condition_expr)

        try:
            result = eval_condition(condition_expr, ctx)
        except Exception as exc:
            return StepResult(
                name=name,
                action="<if>",
                state="error",
                error=f"Condition evaluation failed: {exc}",
            )

        branch = step.get("then", []) if result else step.get("else", [])
        if branch:
            # Create a child context that inherits from parent but doesn't
            # pollute the parent's ``prev`` during nested execution
            child_ctx = dict(ctx)
            self._execute_steps(branch, child_ctx, top_level=False)
            # Propagate the last ``prev`` from the branch back up
            ctx["prev"] = child_ctx["prev"]

        return StepResult(
            name=name,
            action="<if>",
            state="ok",
            output=f"condition={condition_expr!r} -> {result} -> {'then' if result else 'else'} branch",
            exit_code=0,
        )

    # ── for_each step ─────────────────────────────────────────────────────────

    def _execute_for_each(self, step: dict, ctx: dict[str, Any]) -> StepResult:
        name = step.get("name", "<for_each>")
        raw_list = step.get("list", [])
        loop_var = step.get("variable", "item")
        body_steps = step.get("steps", [])

        # Resolve the list — may be a template reference or a literal list
        items = resolve_value(raw_list, ctx)
        if isinstance(items, str):
            # Comma-separated fallback for simple string values
            items = [i.strip() for i in items.split(",") if i.strip()]
        if not isinstance(items, list):
            items = [items]

        logger.debug("step[for_each] name=%r var=%r items=%d", name, loop_var, len(items))

        iteration_outputs: list[str] = []
        for i, item in enumerate(items):
            iter_ctx = dict(ctx)
            iter_ctx[loop_var] = item
            iter_ctx["loop"] = {"index": i, "item": item, "length": len(items)}
            self._execute_steps(body_steps, iter_ctx, top_level=False)
            last_prev = iter_ctx.get("prev", {})
            if last_prev.get("state") == "error":
                iteration_outputs.append(
                    f"[{i}] {item}: ERROR — {last_prev.get('error', '')}"
                )
            else:
                iteration_outputs.append(f"[{i}] {item}: {last_prev.get('output', 'ok')}")

        return StepResult(
            name=name,
            action="<for_each>",
            state="ok",
            output="\n".join(iteration_outputs),
            exit_code=0,
        )
