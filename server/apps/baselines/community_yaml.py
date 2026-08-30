"""Baseline ⇄ community YAML.

The schema is ``schemas/baseline.md`` in Vigil-Approved-Scripts. The shape
that matters here is the step reference: a baseline in the repo names its
tasks by **slug**, while a Baseline on the server holds foreign keys to
TaskDefinition rows. Export slugifies the definition's name; import resolves
the slug back against the operator's own library.

Import deliberately does **not** create the tasks it cannot find. A baseline
whose steps silently vanished would import as a baseline that does nothing,
which is worse than a refusal — so a missing slug is an error that names
every slug it could not resolve, and the operator forks those tasks first.
"""

from __future__ import annotations

from typing import Any

from vigil.contentyaml import (
    ContentYamlError,
    check_author,
    dump,
    load_mapping,
    require_str,
    require_tag_list,
    slugify,
)

_WHAT = "baseline"
_MAX_STEPS = 32


def to_yaml(baseline, *, author: str = "", created=None) -> str:
    """Serialise *baseline* into the community repo's dialect."""
    fields: dict[str, Any] = {"name": baseline.name}
    if author:
        fields["author"] = author
    if created is not None:
        # Left as a date object, not a string: PyYAML emits it unquoted,
        # matching every file already in the repo.
        fields["created"] = created
    if baseline.description:
        fields["description"] = baseline.description
    if baseline.target_tags:
        fields["target_tags"] = list(baseline.target_tags)
    if baseline.allow_high_risk:
        # Only emitted when true. The schema defaults it to false, and a file
        # that spells out every default is harder to review than one that
        # states only what is unusual about it.
        fields["allow_high_risk"] = True

    steps = []
    # Numbered 1..N by position, not by the stored ``order`` column. The server
    # writes that column 0-based as an internal sort key, while the community
    # schema's ``order`` is 1-based — exporting the raw value emitted
    # ``order: 0`` for the first step of any normally-created baseline, which
    # this module's own parser then rejected. What the file needs is the
    # sequence, and the rows are already fetched in it.
    ordered = baseline.steps.select_related("definition").order_by("order")
    for position, step in enumerate(ordered, start=1):
        entry: dict[str, Any] = {
            "task": slugify(step.definition.name, fallback="task"),
            "order": position,
        }
        if step.params_override:
            entry["params_override"] = step.params_override
        steps.append(entry)
    fields["steps"] = steps
    return dump(fields)


def parse(text: str) -> dict[str, Any]:
    """Validate community baseline YAML into a plain dict.

    Returns the parsed fields with ``steps`` as a list of
    ``{"task": slug, "order": int, "params_override": dict}``. Resolving those
    slugs against a library is :func:`resolve_steps`, kept separate so the
    document can be validated without a database.
    """
    raw = load_mapping(text, _WHAT)

    name = require_str(raw, "name", _WHAT, max_len=120)
    description = require_str(raw, "description", _WHAT, max_len=2000,
                              required=False)
    author = require_str(raw, "author", _WHAT, max_len=80, required=False)
    if author:
        author = check_author(author, _WHAT)
    target_tags = require_tag_list(raw, "target_tags", _WHAT)

    allow_high_risk = raw.get("allow_high_risk", False)
    if not isinstance(allow_high_risk, bool):
        raise ContentYamlError(
            f"{_WHAT}: 'allow_high_risk' must be true or false.")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ContentYamlError(
            f"{_WHAT}: 'steps' is required and must list at least one task.")
    if len(raw_steps) > _MAX_STEPS:
        raise ContentYamlError(
            f"{_WHAT}: at most {_MAX_STEPS} steps ({len(raw_steps)} given).")

    steps: list[dict[str, Any]] = []
    seen_orders: set[int] = set()
    for position, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, dict):
            raise ContentYamlError(
                f"{_WHAT}: step {position} must be a mapping with a 'task' key.")
        slug = entry.get("task")
        if not isinstance(slug, str) or not slug.strip():
            raise ContentYamlError(
                f"{_WHAT}: step {position} is missing 'task'.")
        order = entry.get("order", position)
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ContentYamlError(
                f"{_WHAT}: step {position} has a non-positive-integer 'order'.")
        if order in seen_orders:
            # Mirrors the server's unique constraint on (baseline, order).
            # Catching it here gives a readable message instead of an
            # IntegrityError from the save.
            raise ContentYamlError(
                f"{_WHAT}: two steps share order {order}; orders must be unique.")
        seen_orders.add(order)
        override = entry.get("params_override") or {}
        if not isinstance(override, dict):
            raise ContentYamlError(
                f"{_WHAT}: step {position} has a non-mapping 'params_override'.")
        steps.append({"task": slug.strip(), "order": order,
                      "params_override": override})

    return {
        "name": name,
        "description": description,
        "author": author,
        "target_tags": target_tags,
        "allow_high_risk": allow_high_risk,
        "steps": steps,
    }


def resolve_steps(steps: list[dict[str, Any]], definitions,
                  names_by_slug: dict[str, str] | None = None
                  ) -> list[dict[str, Any]]:
    """Map each step's slug onto a TaskDefinition from *definitions*.

    *definitions* is any iterable of TaskDefinition rows — the caller decides
    what the operator is allowed to see, so this never queries. Raises with
    every unresolved slug at once: fixing them one error at a time would be a
    miserable way to import a twelve-step baseline.

    A slug in the repo is a *filename*, and the repo does not require that it
    equal ``slugify(name)``. So the match is tried twice: first by slugifying
    each library task's name, then — for anything still missing — by looking
    the slug up in *names_by_slug* (the community index) and matching the name
    it maps to. Without that second pass a baseline could refuse to import
    while the operator was looking at the very task it wanted.
    """
    by_slug: dict[str, Any] = {}
    by_name: dict[str, Any] = {}
    for definition in definitions:
        by_slug.setdefault(slugify(definition.name, fallback="task"), definition)
        by_name.setdefault(definition.name.strip().lower(), definition)

    resolved, missing = [], []
    for step in steps:
        definition = by_slug.get(step["task"])
        if definition is None and names_by_slug:
            wanted = names_by_slug.get(step["task"], "").strip().lower()
            definition = by_name.get(wanted) if wanted else None
        if definition is None:
            missing.append(step["task"])
            continue
        resolved.append({**step, "definition": definition})

    if missing:
        raise ContentYamlError(
            "This baseline references tasks that are not in your library: "
            + ", ".join(sorted(set(missing)))
            + ". Fork them from Community first, then import the baseline.")
    return resolved
