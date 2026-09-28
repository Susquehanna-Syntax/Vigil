"""Playbook ⇄ community YAML.

The schema is ``schemas/playbook.md`` in Vigil-Approved-Scripts. The shape
that matters here is the step reference: a playbook in the repo names its
tasks by **slug**, while a Playbook on the server holds foreign keys to
TaskDefinition rows. Export slugifies the definition's name; import resolves
the slug back against the operator's own library.

Import deliberately does **not** create the tasks it cannot find. A playbook
whose steps silently vanished would import as a playbook that does nothing,
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
    new_uid,
    parse_uid,
    require_str,
    require_tag_list,
    slugify,
)

_WHAT = "playbook"
_MAX_STEPS = 32


def to_yaml(playbook, *, author: str = "", created=None) -> str:
    """Serialise *playbook* into the community repo's dialect."""
    fields: dict[str, Any] = {"name": playbook.name}
    # The playbook's own identity in the catalog. Minted on first export and
    # stored, so exporting the same playbook twice does not produce two
    # different pieces of content.
    if not playbook.community_uid:
        playbook.community_uid = new_uid()
        playbook.save(update_fields=["community_uid"])
    fields["uid"] = str(playbook.community_uid)
    if author:
        fields["author"] = author
    if created is not None:
        # Left as a date object, not a string: PyYAML emits it unquoted,
        # matching every file already in the repo.
        fields["created"] = created
    if playbook.description:
        fields["description"] = playbook.description
    if playbook.target_tags:
        fields["target_tags"] = list(playbook.target_tags)
    if playbook.allow_high_risk:
        # Only emitted when true. The schema defaults it to false, and a file
        # that spells out every default is harder to review than one that
        # states only what is unusual about it.
        fields["allow_high_risk"] = True

    def step_entry(step, position: int) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "task": slugify(step.definition.name, fallback="task"),
            "order": position,
        }
        if playbook.flow:
            # A branching playbook names its steps: its conditions refer to them.
            entry["id"] = step.step_id
        # The slug stays because it is what a reviewer reading the diff can
        # follow; the uid is what actually resolves on the far side.
        if step.definition.community_uid:
            entry["uid"] = str(step.definition.community_uid)
        if step.params_override:
            entry["params_override"] = step.params_override
        # Only emitted when it is not the default, like allow_high_risk.
        if step.on_not_applicable != "stop":
            entry["on_not_applicable"] = step.on_not_applicable
        if step.on_failure != "stop":
            entry["on_failure"] = step.on_failure
        return entry

    # Numbered 1..N by position, not by the stored ``order`` column. The server
    # writes that column 0-based as an internal sort key, while the community
    # schema's ``order`` is 1-based — exporting the raw value emitted
    # ``order: 0`` for the first step of any normally-created playbook, which
    # this module's own parser then rejected.
    ordered = list(playbook.steps.select_related("definition").order_by("order"))
    positions = {s.step_id: i for i, s in enumerate(ordered, start=1)}
    if playbook.flow:
        from .flow import tree_from

        def emit(nodes: list) -> list:
            out = []
            for node in nodes:
                if "step" in node:
                    out.append(step_entry(node["step"], positions[node["step"].step_id]))
                else:
                    branch: dict[str, Any] = {"if": node["if"], "then": emit(node["then"])}
                    if node["else"]:
                        branch["else"] = emit(node["else"])
                    out.append(branch)
            return out

        steps = emit(tree_from(playbook))
    else:
        steps = [step_entry(step, position)
                 for position, step in enumerate(ordered, start=1)]
    fields["steps"] = steps
    return dump(fields)


def parse(text: str) -> dict[str, Any]:
    """Validate community playbook YAML into a plain dict.

    Returns the parsed fields with ``steps`` as a list of
    ``{"task": slug, "order": int, "params_override": dict}``. Resolving those
    slugs against a library is :func:`resolve_steps`, kept separate so the
    document can be validated without a database.
    """
    raw = load_mapping(text, _WHAT)

    uid = parse_uid(raw, _WHAT)
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
    has_branch = False

    def parse_step(entry: dict, position: int) -> dict[str, Any]:
        slug = entry.get("task")
        if not isinstance(slug, str) or not slug.strip():
            raise ContentYamlError(
                f"{_WHAT}: step {position} is missing 'task'.")
        order = entry.get("order", position)
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ContentYamlError(
                f"{_WHAT}: step {position} has a non-positive-integer 'order'.")
        if order in seen_orders:
            # Mirrors the server's unique constraint on (playbook, order).
            # Catching it here gives a readable message instead of an
            # IntegrityError from the save.
            raise ContentYamlError(
                f"{_WHAT}: two steps share order {order}; orders must be unique.")
        seen_orders.add(order)
        override = entry.get("params_override") or {}
        if not isinstance(override, dict):
            raise ContentYamlError(
                f"{_WHAT}: step {position} has a non-mapping 'params_override'.")
        on_not_applicable = "stop"
        if "on_not_applicable" in entry:
            value = entry["on_not_applicable"]
            if not isinstance(value, str) or value not in ("skip", "stop"):
                raise ContentYamlError(
                    f"{_WHAT}: step {position} has an invalid "
                    "'on_not_applicable' (must be 'skip' or 'stop').")
            on_not_applicable = value
        on_failure = entry.get("on_failure", "stop")
        if on_failure not in ("stop", "continue"):
            raise ContentYamlError(
                f"{_WHAT}: step {position} has an invalid 'on_failure' "
                "(must be 'stop' or 'continue').")
        step_id = entry.get("id")
        if step_id is not None and not isinstance(step_id, str):
            # YAML reads a bare yes / no / on / off as true / false: quote it.
            raise ContentYamlError(
                f"{_WHAT}: step {position} has a non-text 'id' ({step_id!r}) — "
                "quote it, e.g. id: \"yes\".")
        return {"task": slug.strip(), "order": order, "id": step_id,
                "uid": parse_uid(entry, f"{_WHAT} step {position}"),
                "params_override": override,
                "on_not_applicable": on_not_applicable,
                "on_failure": on_failure}

    def walk(items: list, where: str) -> list:
        """The steps as a tree: each node an index into ``steps`` or a branch."""
        nonlocal has_branch
        nodes = []
        for entry in items:
            if not isinstance(entry, dict):
                raise ContentYamlError(
                    f"{_WHAT}: {where} entries must be a mapping with a 'task' "
                    "key, or an if/then/else.")
            if "if" in entry:
                has_branch = True
                then_items = entry.get("then")
                if not isinstance(then_items, list) or not then_items:
                    raise ContentYamlError(
                        f"{_WHAT}: a branch's 'then' must list at least one step.")
                else_items = entry.get("else") or []
                if not isinstance(else_items, list):
                    raise ContentYamlError(f"{_WHAT}: a branch's 'else' must be a list.")
                nodes.append({"if": entry["if"], "then": walk(then_items, "then"),
                              "else": walk(else_items, "else")})
                continue
            if len(steps) >= _MAX_STEPS:
                raise ContentYamlError(f"{_WHAT}: at most {_MAX_STEPS} steps.")
            steps.append(parse_step(entry, len(steps) + 1))
            nodes.append({"step": len(steps) - 1})
        return nodes

    tree = walk(raw_steps, "steps")
    return {
        "uid": uid,
        "name": name,
        "description": description,
        "author": author,
        "target_tags": target_tags,
        "allow_high_risk": allow_high_risk,
        "steps": steps,
        # The authored if/then/else tree over indexes into ``steps`` (M6 08b),
        # or None for a plain ordered playbook.
        "tree": tree if has_branch else None,
    }


def resolve_steps(steps: list[dict[str, Any]], definitions,
                  names_by_slug: dict[str, str] | None = None
                  ) -> list[dict[str, Any]]:
    """Map each step's slug onto a TaskDefinition from *definitions*.

    *definitions* is any iterable of TaskDefinition rows — the caller decides
    what the operator is allowed to see, so this never queries. Raises with
    every unresolved slug at once: fixing them one error at a time would be a
    miserable way to import a twelve-step playbook.

    A slug in the repo is a *filename*, and the repo does not require that it
    equal ``slugify(name)``. So the match is tried twice: first by slugifying
    each library task's name, then — for anything still missing — by looking
    the slug up in *names_by_slug* (the community index) and matching the name
    it maps to. Without that second pass a playbook could refuse to import
    while the operator was looking at the very task it wanted.
    """
    by_uid: dict[str, Any] = {}
    by_slug: dict[str, Any] = {}
    by_name: dict[str, Any] = {}
    for definition in definitions:
        if definition.community_uid:
            by_uid.setdefault(str(definition.community_uid), definition)
        by_slug.setdefault(slugify(definition.name, fallback="task"), definition)
        by_name.setdefault(definition.name.strip().lower(), definition)

    resolved, missing = [], []
    for step in steps:
        # uid first: it survives a rename on either side and does not care
        # whether two tasks share a name. The slug passes below are what keep
        # files written before uids existed working.
        definition = by_uid.get(step.get("uid") or "")
        if definition is None:
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
            "This playbook references tasks that are not in your library: "
            + ", ".join(sorted(set(missing)))
            + ". Fork them from Community first, then import the playbook.")
    return resolved
