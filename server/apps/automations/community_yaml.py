"""Automation ⇄ community YAML.

The schema is ``schemas/automation.md`` in Vigil-Approved-Scripts. Two things
about it shape this module:

* The action is named by **slug** — ``task: install-nginx`` or
  ``baseline: container-host-maintenance`` — exactly as baselines name their
  steps, and resolved the same way on import.
* ``target: host`` and ``event_host`` are **not** representable. Both name a
  specific host by primary key, and a key from someone else's server means
  nothing here. Export refuses rather than emitting a file that would import
  pointing at an arbitrary host, and import rejects the value outright.

The event filters that are pure text — ``match_text`` and friends — travel
fine and are carried through, because an automation that fires on the wrong
alerts is exactly the kind of thing a shared file should bring with it.
"""

from __future__ import annotations

import re
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

_WHAT = "automation"

_CRON_FIELDS = ("minute", "hour", "dom", "month", "dow")
#: Only the characters cron itself understands. These strings reach a
#: scheduler, so a permissive field here is a place to hide something that is
#: not a schedule — see the schema doc.
_CRON_RE = re.compile(r"^[0-9*,\-/]{1,64}$")

_SEVERITIES = ("info", "warning", "critical")
_PORTABLE_TARGETS = ("event_host", "tags", "all")


def to_yaml(automation, *, author: str = "", created=None) -> str:
    """Serialise *automation* into the community repo's dialect.

    Raises when the automation names a specific host: that is not a lossy
    export, it is an unrepresentable one, and emitting it anyway would produce
    a file that quietly targets whatever host it lands next to.
    """
    if automation.target == "host":
        raise ContentYamlError(
            "This automation targets one specific host, which cannot be shared "
            "— a host id means nothing on someone else's server. Switch it to "
            "tags or all before submitting.")
    if automation.event_host_id:
        raise ContentYamlError(
            "This automation only watches one specific host, which cannot be "
            "shared. Clear that filter, or narrow it with event tags instead, "
            "before submitting.")

    fields: dict[str, Any] = {"name": automation.name}
    if not automation.community_uid:
        automation.community_uid = new_uid()
        automation.save(update_fields=["community_uid"])
    fields["uid"] = str(automation.community_uid)
    if author:
        fields["author"] = author
    if created is not None:
        # Left as a date object, not a string: PyYAML emits it unquoted,
        # matching every file already in the repo.
        fields["created"] = created
    fields["enabled"] = automation.enabled

    fields["trigger"] = automation.trigger
    if automation.trigger == "event":
        fields["event"] = automation.event
        if automation.min_severity:
            fields["min_severity"] = automation.min_severity
        if automation.event_tags:
            fields["event_tags"] = list(automation.event_tags)
        if automation.match_text:
            fields["match_text"] = automation.match_text
            fields["match_field"] = automation.match_field
            fields["match_mode"] = automation.match_mode
    else:
        fields["cron"] = {
            "minute": automation.cron_minute,
            "hour": automation.cron_hour,
            "dom": automation.cron_dom,
            "month": automation.cron_month,
            "dow": automation.cron_dow,
        }

    fields["action_kind"] = automation.action_kind
    if automation.action_kind == "baseline":
        if not automation.baseline_id:
            raise ContentYamlError(
                "This automation runs a baseline but no baseline is set.")
        fields["baseline"] = slugify(automation.baseline.name, fallback="baseline")
        if automation.baseline.community_uid:
            fields["action_uid"] = str(automation.baseline.community_uid)
    else:
        if not automation.task_definition_id:
            raise ContentYamlError(
                "This automation runs a task but no task is set.")
        fields["task"] = slugify(automation.task_definition.name, fallback="task")
        if automation.task_definition.community_uid:
            fields["action_uid"] = str(automation.task_definition.community_uid)
        if automation.params_override:
            fields["params_override"] = automation.params_override

    fields["target"] = automation.target
    if automation.target == "tags":
        fields["target_tags"] = list(automation.target_tags)
    return dump(fields)


def parse(text: str) -> dict[str, Any]:
    """Validate community automation YAML into a plain dict."""
    raw = load_mapping(text, _WHAT)

    uid = parse_uid(raw, _WHAT)
    name = require_str(raw, "name", _WHAT, max_len=120)
    description = require_str(raw, "description", _WHAT, max_len=2000,
                              required=False)
    author = require_str(raw, "author", _WHAT, max_len=80, required=False)
    if author:
        author = check_author(author, _WHAT)

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ContentYamlError(f"{_WHAT}: 'enabled' must be true or false.")

    trigger = require_str(raw, "trigger", _WHAT, max_len=12)
    if trigger not in ("event", "schedule"):
        raise ContentYamlError(
            f"{_WHAT}: 'trigger' must be 'event' or 'schedule', not {trigger!r}.")

    out: dict[str, Any] = {
        "uid": uid,
        # The uid of the task or baseline this automation runs, when the file
        # carries one. Named separately from `uid` so a file cannot confuse
        # its own identity with its action's.
        "action_uid": parse_uid({"uid": raw.get("action_uid")}, f"{_WHAT} action"),
        "name": name, "description": description, "author": author,
        "enabled": enabled, "trigger": trigger,
        "event": "", "min_severity": "", "event_tags": [],
        "match_text": "", "match_field": "any", "match_mode": "contains",
        "cron": {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"},
    }

    if trigger == "event":
        out["event"] = require_str(raw, "event", _WHAT, max_len=40)
        severity = require_str(raw, "min_severity", _WHAT, max_len=10,
                               required=False)
        if severity and severity not in _SEVERITIES:
            raise ContentYamlError(
                f"{_WHAT}: 'min_severity' must be one of "
                f"{', '.join(_SEVERITIES)}.")
        out["min_severity"] = severity
        out["event_tags"] = require_tag_list(raw, "event_tags", _WHAT)
        out["match_text"] = require_str(raw, "match_text", _WHAT, max_len=200,
                                        required=False)
        if out["match_text"]:
            out["match_field"] = require_str(raw, "match_field", _WHAT,
                                             max_len=10, required=False,
                                             default="any") or "any"
            out["match_mode"] = require_str(raw, "match_mode", _WHAT,
                                            max_len=16, required=False,
                                            default="contains") or "contains"
    else:
        cron = raw.get("cron") or {}
        if not isinstance(cron, dict):
            raise ContentYamlError(f"{_WHAT}: 'cron' must be a mapping.")
        unknown = set(cron) - set(_CRON_FIELDS)
        if unknown:
            raise ContentYamlError(
                f"{_WHAT}: unknown cron field(s): {', '.join(sorted(unknown))}.")
        for field in _CRON_FIELDS:
            value = cron.get(field, out["cron"][field])
            if isinstance(value, int) and not isinstance(value, bool):
                # `minute: 0` is the natural thing to write and YAML makes it
                # an int. Accept it rather than making people quote numbers.
                value = str(value)
            if not isinstance(value, str) or not _CRON_RE.match(value):
                raise ContentYamlError(
                    f"{_WHAT}: cron '{field}' must be a cron field of digits, "
                    f"*, comma, hyphen or slash — got {value!r}.")
            out["cron"][field] = value

    action_kind = require_str(raw, "action_kind", _WHAT, max_len=12)
    if action_kind not in ("task", "baseline"):
        raise ContentYamlError(
            f"{_WHAT}: 'action_kind' must be 'task' or 'baseline'.")
    out["action_kind"] = action_kind

    task_slug = raw.get("task")
    baseline_slug = raw.get("baseline")
    if action_kind == "task":
        if baseline_slug is not None:
            raise ContentYamlError(
                f"{_WHAT}: 'baseline' is not allowed when action_kind is 'task'.")
        out["slug"] = require_str(raw, "task", _WHAT, max_len=60)
    else:
        if task_slug is not None:
            raise ContentYamlError(
                f"{_WHAT}: 'task' is not allowed when action_kind is 'baseline'.")
        out["slug"] = require_str(raw, "baseline", _WHAT, max_len=60)

    override = raw.get("params_override") or {}
    if not isinstance(override, dict):
        raise ContentYamlError(f"{_WHAT}: 'params_override' must be a mapping.")
    out["params_override"] = override

    target = require_str(raw, "target", _WHAT, max_len=12, required=False,
                         default="event_host") or "event_host"
    if target == "host":
        raise ContentYamlError(
            f"{_WHAT}: 'target: host' names one machine by id and cannot be "
            f"shared. Use 'tags' or 'all'.")
    if target not in _PORTABLE_TARGETS:
        raise ContentYamlError(
            f"{_WHAT}: 'target' must be one of "
            f"{', '.join(_PORTABLE_TARGETS)}, not {target!r}.")
    out["target"] = target

    target_tags = require_tag_list(raw, "target_tags", _WHAT)
    if target == "tags" and not target_tags:
        raise ContentYamlError(
            f"{_WHAT}: 'target: tags' needs at least one entry in "
            f"'target_tags', or it would match no hosts at all.")
    if target != "tags" and target_tags:
        raise ContentYamlError(
            f"{_WHAT}: 'target_tags' only applies when target is 'tags'.")
    out["target_tags"] = target_tags
    return out


def _match(rows, slug: str, fallback: str, names_by_slug, uid: str = ""):
    """Find the row a community reference points at.

    uid first — it survives a rename on either side and does not require names
    to be unique. The two slug passes below keep files written before uids
    existed working: a slug is a *filename*, and the catalog does not require
    it to equal ``slugify(name)``, so the community index translates the rest.
    """
    if uid:
        for row in rows:
            if row.community_uid and str(row.community_uid) == uid:
                return row
    for row in rows:
        if slugify(row.name, fallback=fallback) == slug:
            return row
    wanted = (names_by_slug or {}).get(slug, "").strip().lower()
    if wanted:
        for row in rows:
            if row.name.strip().lower() == wanted:
                return row
    return None


def resolve_action(parsed: dict[str, Any], *, definitions, baselines,
                   task_names_by_slug=None, baseline_names_by_slug=None):
    """Resolve the parsed automation's action slug to a real row.

    Returns ``(task_definition, baseline)`` with exactly one of them set.
    """
    if parsed["action_kind"] == "task":
        definition = _match(definitions, parsed["slug"], "task",
                            task_names_by_slug, parsed.get("action_uid", ""))
        if definition is not None:
            return definition, None
        raise ContentYamlError(
            f"This automation runs the task '{parsed['slug']}', which is not in "
            f"your library. Fork it from Community first.")
    baseline = _match(baselines, parsed["slug"], "baseline",
                      baseline_names_by_slug, parsed.get("action_uid", ""))
    if baseline is not None:
        return None, baseline
    raise ContentYamlError(
        f"This automation runs the baseline '{parsed['slug']}', which you do "
        f"not have. Fork it from Community first.")
