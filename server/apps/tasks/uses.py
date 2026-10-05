"""``use: <task>`` inside an if/then/else branch (M6 phase 05b).

A branch may name another task instead of repeating its steps. At deploy the
server copies that task's *current* steps into the branch and signs the whole
tree, so a later edit to the named task never changes a run already deployed.

The work happens on the raw YAML: each ``{"use": name}`` item in a
``then``/``else`` list is replaced, in place, by the items of the named task's
own ``actions`` list (its steps and its own branches), recursively, and the
composite is then judged by ``parse_and_validate`` like any hand-written task —
unique ids, nesting, the step cap, step references and risk are all checked on
what will actually ship.
"""
from __future__ import annotations

import yaml

MAX_DEPTH = 5


class UseError(ValueError):
    """A ``use:`` reference that cannot be satisfied."""


def resolve_use(name: str, user):
    """The task a ``use:`` names, as seen by ``user``.

    The user's own task with that name wins; otherwise a community task with
    it. No match, or several community matches, is an error.
    """
    from .models import TaskDefinition

    own = TaskDefinition.objects.filter(name__iexact=name, owner=user).order_by("created_at")
    if user is not None and getattr(user, "pk", None) and own.exists():
        return own.first()
    community = list(TaskDefinition.objects.filter(
        name__iexact=name, owner=None,
        visibility=TaskDefinition.Visibility.COMMUNITY,
    )[:2])
    if not community:
        raise UseError(f"use: no task named {name!r}")
    if len(community) > 1:
        raise UseError(f"use: {name!r} is ambiguous — several community tasks share that name")
    return community[0]


def _raw(definition) -> dict:
    raw = yaml.safe_load(definition.yaml_source or "") or {}
    if not isinstance(raw, dict):
        raise UseError(f"use: task {definition.name!r} is not a valid task")
    return raw


def expand_uses(yaml_source: str, user, audit: list | None = None) -> str:
    """Return ``yaml_source`` with every branch ``use:`` replaced by the used
    task's current actions. ``audit``, if given, collects
    ``{"name", "id", "updated_at"}`` for every task copied in."""
    raw = yaml.safe_load(yaml_source) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        return yaml_source  # parse_and_validate reports the real problem

    def splice(items: list, chain: tuple[str, ...]) -> list:
        out = []
        for item in items:
            if isinstance(item, dict) and set(item) == {"use"}:
                out.extend(expand(str(item["use"]).strip(), chain))
            elif isinstance(item, dict) and "if" in item:
                branch = dict(item)
                for arm in ("then", "else"):
                    if isinstance(branch.get(arm), list):
                        branch[arm] = splice(branch[arm], chain)
                out.append(branch)
            else:
                out.append(item)
        return out

    def expand(name: str, chain: tuple[str, ...]) -> list:
        key = name.lower()
        if key in chain:
            loop = " → ".join([*chain, key])
            raise UseError(f"use cycle: {loop}")
        if len(chain) >= MAX_DEPTH:
            raise UseError(f"use: nested deeper than {MAX_DEPTH} tasks")
        definition = resolve_use(name, user)
        used = _raw(definition)
        if used.get("inputs"):
            raise UseError(
                f"task {definition.name!r} cannot be used in a branch: it declares inputs")
        if used.get("relevant"):
            raise UseError(
                f"task {definition.name!r} cannot be used in a branch: it declares relevant:")
        actions = used.get("actions")
        if not isinstance(actions, list) or not actions:
            raise UseError(f"task {definition.name!r} has no actions to use")
        if audit is not None:
            audit.append({
                "name": definition.name,
                "id": str(definition.id),
                "updated_at": definition.updated_at.isoformat() if getattr(
                    definition, "updated_at", None) else None,
            })
        return splice(actions, (*chain, key))

    raw["actions"] = splice(raw["actions"], ())
    return yaml.safe_dump(raw, sort_keys=False)
