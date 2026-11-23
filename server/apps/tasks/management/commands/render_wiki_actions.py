"""Regenerate the wiki's Built-in Actions reference from ACTION_REGISTRY.

The reference used to be hand-maintained HTML, and it drifted: four actions
that do not exist were documented for months, and ``restart_service`` was
given a param name it does not take. A definition written from that page
failed validation on save, which is the worst place to find out the docs were
wrong.

This command writes the section instead. It reads ACTION_REGISTRY, builds one
disclosure per action with a complete task definition the reader can copy, and
replaces the block between the two marker comments in the wiki. Run it after
changing ACTION_REGISTRY:

    manage.py render_wiki_actions

Pass ``--check`` to fail instead of writing, which is what CI wants.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tasks.spec import ACTION_REGISTRY, action_outputs

# The tables live in apps.tasks.wiki_actions; they are imported by name here
# both for the rendering below and because other modules and tests import
# them from this command (agentprofiles.views reads GROUPS, the tests read
# BEGIN, END, example_yaml and OUTPUT_NOTES).
from apps.tasks.wiki_actions.examples import EXAMPLE_TITLES, example_yaml
from apps.tasks.wiki_actions.groups import GROUP_NOTES, GROUPS
from apps.tasks.wiki_actions.outputs import OUTPUT_NOTES
from apps.tasks.wiki_actions.params import (  # noqa: F401
    ACTION_PARAM_NOTES,
    EXAMPLE_INPUTS,
    PARAM_EXAMPLES,
    PARAM_NOTES,
)

BEGIN = "<!-- BEGIN GENERATED: built-in actions (manage.py render_wiki_actions) -->"
END = "<!-- END GENERATED: built-in actions -->"


def _check_output_notes() -> None:
    missing = [f"{action}.{field}" for action in ACTION_REGISTRY
               for field in action_outputs(action)
               if field not in OUTPUT_NOTES.get(action, {})]
    if missing:
        raise CommandError(
            "declared outputs with no entry in OUTPUT_NOTES: "
            + ", ".join(sorted(missing)))


def _group_actions() -> list[tuple[str, str, list[str]]]:
    """GROUPS, checked against the registry.

    A new action that nobody added to GROUPS would otherwise vanish from the
    page while every existing test still passed, which is the exact failure
    this file exists to prevent.
    """
    placed = [name for _, _, names in GROUPS for name in names]
    duplicated = {n for n in placed if placed.count(n) > 1}
    if duplicated:
        raise CommandError(f"listed in two groups: {sorted(duplicated)}")
    missing = sorted(set(ACTION_REGISTRY) - set(placed))
    if missing:
        raise CommandError(
            f"not in any group in apps/tasks/wiki_actions/groups.py: {missing}. "
            "Add them to a group so they appear in the wiki.")
    unknown = sorted(set(placed) - set(ACTION_REGISTRY))
    if unknown:
        raise CommandError(f"grouped but not in ACTION_REGISTRY: {unknown}")
    _check_output_notes()
    return GROUPS


def _yaml_html(source: str) -> str:
    """YAML with the wiki's key/string highlighting spans."""
    out = []
    for line in source.rstrip("\n").split("\n"):
        match = re.match(r"^(\s*)(- )?([A-Za-z_][\w]*): ?(.*)$", line)
        if not match:
            out.append(html.escape(line))
            continue
        indent, dash, key, value = match.groups()
        piece = indent + (dash or "")
        piece += f'<span class="hl-key">{html.escape(key)}</span>: '
        if value:
            piece += f'<span class="hl-string">{html.escape(value)}</span>'
        out.append(piece.rstrip())
    return "\n".join(out)


def _params_table(entry: dict, action: str = "") -> str:
    def note(param: str) -> str:
        return ACTION_PARAM_NOTES.get((action, param), PARAM_NOTES.get(param, ""))

    rows = []
    for param in entry["required"]:
        rows.append(
            f'<tr><td><code>{param}</code></td><td><span class="param-req">'
            f'required</span></td><td>{html.escape(note(param))}'
            f"</td></tr>")
    for param in entry["optional"]:
        rows.append(
            f'<tr><td><code>{param}</code></td><td><span class="param-opt">'
            f'optional</span></td><td>{html.escape(note(param))}'
            f"</td></tr>")
    if not rows:
        return '<p class="action-noparams">This action takes no params.</p>'
    return (
        '<div class="table-wrap"><table class="param-table">'
        "<thead><tr><th>Param</th><th></th><th>Meaning</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")


def _outputs_table(action: str) -> str:
    """What a later step can read from this one: status always, plus outputs."""
    outputs = action_outputs(action)
    rows = [
        '<tr><td><code>status</code></td><td>str</td>'
        "<td>ok or skipped, as a later step sees it (a failed step stops the task).</td></tr>"]
    for field, kind in outputs.items():
        rows.append(
            f"<tr><td><code>{html.escape(field)}</code></td><td>{html.escape(kind)}</td>"
            f"<td>{html.escape(OUTPUT_NOTES[action][field])}</td></tr>")
    table = (
        '<div class="table-wrap"><table class="param-table">'
        "<thead><tr><th>Output</th><th>Type</th><th>Meaning</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")
    if outputs:
        table += ('<p class="prose">Read one later as '
                  "<code>${{ steps.&lt;id&gt;.result.&lt;field&gt; }}</code>.</p>")
    return table


def render() -> str:
    parts = [BEGIN]
    parts.append(
        '<div class="action-toolbar">\n'
        '  <input type="search" id="action-filter" class="action-search" '
        'placeholder="Filter actions by name or param" '
        'aria-label="Filter actions">\n'
        '  <select id="action-group" class="action-select" '
        'aria-label="Filter by category">\n'
        '    <option value="">All categories</option>\n'
        + "".join(
            f'    <option value="{slug}">{html.escape(title)}</option>\n'
            for slug, title, _ in GROUPS)
        + '  </select>\n'
        '  <select id="action-risk" class="action-select" '
        'aria-label="Filter by risk">\n'
        '    <option value="">All risk tiers</option>\n'
        '    <option value="low">Low risk</option>\n'
        '    <option value="standard">Standard risk</option>\n'
        '    <option value="high">High risk</option>\n'
        '  </select>\n'
        '</div>\n'
        '<p class="action-count" id="action-count"></p>')

    for slug, title, names in _group_actions():
        parts.append(f'<div class="action-group" data-group="{slug}">')
        parts.append(f'<h4 class="action-group-title">{html.escape(title)}</h4>')
        note = GROUP_NOTES.get(slug)
        if note:
            parts.append(f'<p class="action-group-note">{html.escape(note)}</p>')
        for action in names:
            entry = ACTION_REGISTRY[action]
            risk = entry["risk"]
            risk_class = {"low": "risk-low", "standard": "risk-std",
                          "high": "risk-high"}[risk]
            _, blurb = EXAMPLE_TITLES.get(action, (None, ""))
            keywords = " ".join([action] + entry["required"] + entry["optional"])
            required = ", ".join(entry["required"]) or "none"
            parts.append(
                f'<details class="action" id="action-{action}" data-group="{slug}" '
                f'data-risk="{risk}" data-keywords="{html.escape(keywords)}">'
                f"<summary>"
                f'<code class="action-name">{action}</code>'
                f'<span class="risk {risk_class}">{risk}</span>'
                f'<span class="action-label">{html.escape(entry["label"])}</span>'
                f'<span class="action-required">{html.escape(required)}</span>'
                f"</summary>"
                f'<div class="action-body">')
            if blurb:
                parts.append(f'<p class="prose">{html.escape(blurb)}</p>')
            parts.append(_params_table(entry, action))
            parts.append(_outputs_table(action))
            parts.append(
                '<div class="code-block"><div class="code-label">YAML</div>'
                '<button class="copy-btn" type="button">Copy</button>'
                f"<pre>{_yaml_html(example_yaml(action))}</pre></div>")
            parts.append("</div></details>")
        parts.append("</div>")

    parts.append('<p class="action-empty" id="action-empty" hidden>'
                 "No action matches that filter.</p>")
    parts.append(END)
    return "\n".join(parts)


class Command(BaseCommand):
    help = ("Regenerate the wiki's Built-in Actions reference from "
            "ACTION_REGISTRY. --check fails instead of writing.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Exit non-zero if the wiki is out of date, and write nothing.")

    def handle(self, *args, **opts):
        path = Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html"
        if not path.is_file():
            raise CommandError(f"wiki not found at {path}")
        text = path.read_text(encoding="utf-8")
        if BEGIN not in text or END not in text:
            raise CommandError(
                "marker comments missing from the wiki. The generated block is "
                f"delimited by:\n  {BEGIN}\n  {END}")

        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        updated = head + render() + tail

        if updated == text:
            self.stdout.write(self.style.SUCCESS("Wiki action reference is up to date."))
            return
        if opts["check"]:
            raise CommandError(
                "wiki action reference is out of date. Run:\n"
                "  manage.py render_wiki_actions")
        path.write_text(updated, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(ACTION_REGISTRY)} actions to {path}"))
