"""If/then/else between a playbook's task steps (M6 phase 08b).

A playbook's steps are authored as a tree: each item is a step (a task) or a
branch ``{"if": <expr>, "then": [...], "else": [...]}``. ``build_flow`` turns
that tree into the flat, ordered step list the playbook stores plus the
``flow`` that records the branches by step id, validating every condition.

A condition may read an earlier step's ``steps.<id>.status`` (ok / failed /
not_applicable / skipped) or ``steps.<id>.result.<field>`` — every output that
step's task produced, merged. Because a condition may only name steps that come
earlier than the branch, and nothing in a branch can change an earlier step's
result, the server can decide each step with a per-step *guard* (``(C)`` for
then, ``not (C)`` for else, nested joined with ``and``) evaluated just before
the step would be released — the same idea the agent uses inside a task.
"""
from __future__ import annotations

import ast
import re
from typing import Any

from apps.tasks.expression import ExprError, parse, referenced_steps
from apps.tasks.registry import action_outputs

MAX_DEPTH = 3
MAX_ELIF = 8
MAX_STEPS = 32
MAX_GOTOS = 8
GOTO_MAX_DEFAULT = 3
GOTO_MAX_LIMIT = 10
_STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,59}$")
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class FlowError(ValueError):
    """An authored playbook tree that cannot be stored."""


def task_outputs(definition) -> dict[str, str]:
    """Every output a task can produce, merged over its actions (later wins)."""
    merged: dict[str, str] = {}
    for action in (definition.parsed_spec or {}).get("actions") or []:
        merged.update(action_outputs(action.get("type", "")))
    return merged


def _check_condition(expr: str, earlier: dict[str, dict[str, str]], where: str) -> None:
    try:
        tree = parse(expr)
    except ExprError as exc:
        raise FlowError(f"{where}: condition rejected: {exc}") from exc
    for step_id, field in referenced_steps(tree):
        if step_id not in earlier:
            raise FlowError(f"{where}: steps.{step_id} is not an earlier playbook step")
        if field and field not in earlier[step_id]:
            raise FlowError(
                f"{where}: step {step_id!r} has no output {field!r}; "
                f"its task produces: {sorted(earlier[step_id]) or 'nothing'}")
    # An ordering comparison on a text or yes/no output could only ever be false.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            if not isinstance(op, _ORDERING):
                continue
            for side in (sides[i], sides[i + 1]):
                if (isinstance(side, ast.Attribute)
                        and isinstance(side.value, ast.Attribute)
                        and side.value.attr == "result"
                        and isinstance(side.value.value, ast.Attribute)):
                    declared = earlier.get(side.value.value.attr, {}).get(side.attr)
                    if declared in ("str", "bool"):
                        raise FlowError(
                            f"{where}: steps.{side.value.value.attr}.result.{side.attr} is a "
                            f"{declared} output; <, <=, >, >= compare numbers")


def build_flow(items: list) -> tuple[list[dict], list | None]:
    """Validate an authored tree and flatten it.

    *items* is a list whose entries are either a step — a dict carrying
    ``definition`` (a TaskDefinition) and optionally ``id``,
    ``params_override`` and ``on_not_applicable`` — or a branch
    ``{"if": str, "then": [...], "else": [...]}``. Returns ``(steps, flow)``:
    the steps in document order, each with ``step_id`` and ``branch`` added,
    and the flow by step id (``None`` when there is no branch).

    A go-to node ``{"goto": <step id>, "max": n}`` is stored in the flow as
    ``{"id": "g<n>", "goto": ..., "max": ...}``. Resolving it at dispatch is
    QA-36's job; this phase only accepts, validates and stores it.
    """
    if not isinstance(items, list) or not items:
        raise FlowError("a playbook needs at least one step")
    steps: list[dict] = []
    earlier: dict[str, dict[str, str]] = {}
    counter = 0
    goto_counter = 0
    gotos: list[tuple[str, str, str, int, str]] = []
    has_branch = False

    def walk(nodes: list, depth: int, path: str, where: str) -> list:
        nonlocal counter, goto_counter, has_branch
        out = []
        for index, node in enumerate(nodes):
            here = f"{where}[{index}]"
            if not isinstance(node, dict):
                raise FlowError(f"{here}: must be a step or an if/then/else")
            if "goto" in node:
                unknown = set(node) - {"goto", "max"}
                if unknown:
                    raise FlowError(f"{here}: a go to is goto/max only, not {sorted(unknown)}")
                if index != len(nodes) - 1:
                    raise FlowError(
                        f"{here}: a go to must be the last item in its lane — "
                        "anything after it could never run")
                target = node.get("goto")
                if not isinstance(target, str) or not target.strip():
                    raise FlowError(f"{here}.goto must name a step id")
                max_ = node.get("max")
                if max_ is None:
                    max_ = GOTO_MAX_DEFAULT
                elif not isinstance(max_, int) or isinstance(max_, bool) \
                        or not 1 <= max_ <= GOTO_MAX_LIMIT:
                    raise FlowError(
                        f"{here}.max must be a whole number from 1 to {GOTO_MAX_LIMIT}")
                if len(gotos) >= MAX_GOTOS:
                    raise FlowError(f"a playbook has at most {MAX_GOTOS} go to nodes")
                has_branch = True
                goto_counter += 1
                gid = f"g{goto_counter}"
                gotos.append((gid, target.strip(), path.rstrip("."), len(steps), here))
                out.append({"id": gid, "goto": target.strip(), "max": max_})
                continue
            if "if" in node:
                has_branch = True
                if depth + 1 > MAX_DEPTH:
                    raise FlowError(f"{here}: branches nested deeper than {MAX_DEPTH} levels")
                unknown = set(node) - {"if", "then", "elif", "else"}
                if unknown:
                    raise FlowError(f"{here}: a branch is if/then/elif/else only, not {sorted(unknown)}")
                cond = node.get("if")
                if not isinstance(cond, str) or not cond.strip():
                    raise FlowError(f"{here}.if must be a non-empty condition")
                then_nodes = node.get("then")
                else_nodes = node.get("else") or []
                if not isinstance(then_nodes, list) or not then_nodes:
                    raise FlowError(f"{here}.then must list at least one step")
                if not isinstance(else_nodes, list):
                    raise FlowError(f"{here}.else must be a list")
                elif_nodes = node.get("elif") or []
                if not isinstance(elif_nodes, list):
                    raise FlowError(f"{here}.elif must be a list")
                if len(elif_nodes) > MAX_ELIF:
                    raise FlowError(f"{here}: at most {MAX_ELIF} or-if exits")
                for arm in elif_nodes:
                    if not isinstance(arm, dict):
                        raise FlowError(f"{here}.elif: an or-if is a mapping of if/then")
                    unknown = set(arm) - {"if", "then"}
                    if unknown:
                        raise FlowError(
                            f"{here}.elif: an or-if is if/then only, not {sorted(unknown)}")
                    arm_cond = arm.get("if")
                    if not isinstance(arm_cond, str) or not arm_cond.strip():
                        raise FlowError(f"{here}.elif: an or-if needs a non-empty 'if'")
                    arm_then = arm.get("then")
                    if not isinstance(arm_then, list) or not arm_then:
                        raise FlowError(f"{here}.elif: an or-if's 'then' must list at least one step")
                counter += 1
                bid = f"b{counter}"
                # Judged against the steps before the branch, before any of its
                # own steps join the "earlier" set.
                _check_condition(cond.strip(), dict(earlier), f"branch {bid}")
                # Every or-if is judged against the same snapshot: a condition
                # may not name a step inside the same decision.
                for k, arm in enumerate(elif_nodes, start=1):
                    _check_condition(arm["if"].strip(), dict(earlier),
                                     f"branch {bid} or-if {k}")
                branch: dict[str, Any] = {
                    "id": bid, "if": cond.strip(),
                    "then": walk(then_nodes, depth + 1, f"{path}{bid}.then.", f"{here}.then"),
                }
                if elif_nodes:
                    branch["elif"] = [
                        {"if": arm["if"].strip(),
                         "then": walk(arm["then"], depth + 1,
                                      f"{path}{bid}.elif{k}.", f"{here}.elif[{k - 1}].then")}
                        for k, arm in enumerate(elif_nodes, start=1)
                    ]
                branch["else"] = walk(else_nodes, depth + 1, f"{path}{bid}.else.", f"{here}.else")
                out.append(branch)
                continue
            definition = node.get("definition")
            if definition is None:
                raise FlowError(f"{here}: a step needs a task")
            step_id = str(node.get("id") or f"s{len(steps) + 1}").strip()
            if not _STEP_ID.match(step_id):
                raise FlowError(
                    f"{here}: step id {step_id!r} must start with a letter and use only "
                    f"letters, digits, - and _ (max 60)")
            if step_id in earlier:
                raise FlowError(f"{here}: duplicate step id {step_id!r}")
            if len(steps) >= MAX_STEPS:
                raise FlowError(f"a playbook has at most {MAX_STEPS} steps")
            steps.append({**node, "step_id": step_id, "branch": path.rstrip(".")})
            earlier[step_id] = task_outputs(definition)
            out.append({"step": step_id})
        return out

    flow = walk(items, 0, "", "steps")
    by_id = {st["step_id"]: st for st in steps}
    for gid, target, lane, _position, _here in gotos:
        target_step = by_id.get(target)
        if target_step is None:
            raise FlowError(f"go to {gid}: {target!r} is not a step in this playbook")
        tb = target_step["branch"]
        if not (tb == "" or tb == lane or lane.startswith(tb + ".")):
            raise FlowError(
                f"go to {gid}: it can jump to a step in its own lane or a lane "
                "around it, not into another branch")
    return steps, (flow if has_branch else None)


def branch_guards(flow: list | None) -> dict[str, tuple[str, str]]:
    """``step_id → (guard expression, branch path)`` for every step in a branch."""
    guards: dict[str, tuple[str, str]] = {}

    def walk(nodes: list, conds: list[tuple[str, str, str]]) -> None:
        for node in nodes:
            if "step" in node:
                if conds:
                    guards[node["step"]] = (
                        " and ".join(term for _b, _s, term in conds),
                        ".".join(f"{b}.{side}" for b, side, _t in conds),
                    )
                continue
            if "goto" in node:
                # A go to inside a branch must not run when its branch was not
                # taken either. Step ids must start with a letter, so the "@"
                # key can never collide with one.
                if conds:
                    guards["@" + node["id"]] = (
                        " and ".join(term for _b, _s, term in conds),
                        ".".join(f"{b}.{side}" for b, side, _t in conds),
                    )
                continue
            arms = arms_of(node)
            walk(arms["then"], [*conds, (node["id"], "then", f"({node['if']})")])
            for k, arm in enumerate(arms["elif"], start=1):
                walk(arm["then"], [*conds, (node["id"], f"elif{k}", or_if_term(arms, k))])
            walk(arms["else"], [*conds, (node["id"], "else", else_term(arms))])

    walk(list(flow or []), [])
    return guards


def arms_of(node: dict) -> dict[str, Any]:
    """A decision's three kinds of arm: ``then``, its or-ifs, ``else``."""
    return {"node": node,
            "then": node.get("then") or [],
            "elif": node.get("elif") or [],
            "else": node.get("else") or []}


def or_if_term(arms: dict, k: int) -> str:
    """The k-th or-if runs when the ``if`` and every earlier or-if said no."""
    conds = [arms["node"]["if"]] + [arm["if"] for arm in arms["elif"]]
    return " and ".join([f"not ({c})" for c in conds[:k]] + [f"({conds[k]})"])


def else_term(arms: dict) -> str:
    """``else`` runs when every exit above it said no."""
    conds = [arms["node"]["if"]] + [arm["if"] for arm in arms["elif"]]
    return " and ".join(f"not ({c})" for c in conds)


def flow_sequence(flow) -> list[dict[str, Any]]:
    """The flow's leaf nodes in document order.

    This is the order QA-36 creates a host's task chain in: steps as
    ``{"step": id}`` and go-tos as ``{"goto": target, "id": gid, "max": n}``,
    a branch's ``then`` items before its ``else`` items, recursively.
    """
    out: list[dict[str, Any]] = []

    def walk(nodes: list) -> None:
        for node in nodes or []:
            if "step" in node:
                out.append({"step": node["step"]})
                continue
            if "goto" in node:
                out.append({"goto": node["goto"], "id": node["id"], "max": node["max"]})
                continue
            walk(node.get("then") or [])
            for arm in node.get("elif") or []:
                walk(arm["then"] or [])
            walk(node.get("else") or [])

    walk(list(flow or []))
    return out


def tree_from(playbook) -> list[dict[str, Any]]:
    """The playbook's steps as an authored tree again (for export and the
    editor): its flow with each ``{"step": id}`` replaced by that step."""
    steps = {s.step_id: s for s in playbook.steps.select_related("definition").order_by("order")}
    if not playbook.flow:
        return [{"step": s} for s in steps.values()]

    def walk(nodes: list) -> list:
        out = []
        for node in nodes:
            if "step" in node:
                if node["step"] in steps:
                    out.append({"step": steps[node["step"]]})
            elif "goto" in node:
                out.append({"goto": node["goto"], "max": node["max"]})
            else:
                branch: dict[str, Any] = {"if": node["if"], "then": walk(node.get("then") or [])}
                if node.get("elif"):
                    branch["elif"] = [{"if": arm["if"], "then": walk(arm["then"] or [])}
                                      for arm in node["elif"]]
                branch["else"] = walk(node.get("else") or [])
                out.append(branch)
        return out

    return walk(playbook.flow)
