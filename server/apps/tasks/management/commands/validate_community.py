"""Validate a Vigil-Approved-Scripts checkout with Vigil's own parsers.

The community repo used to ship a second implementation of Vigil's rules, and
it drifted: files its own CI greenlit were rejected by the server's Community
tab. This command is the authority the repo's CI calls, so a file that passes
here is a file the server can read. It needs no database — references are
resolved against the checkout's own files, which is what a fork of the
checkout will have.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from django.core.management.base import BaseCommand, CommandError

from apps.automations.apps import EVENTS
from apps.automations.community_yaml import parse as parse_automation
from apps.playbooks.community_yaml import parse as parse_playbook
from apps.tasks.spec import SpecError, parse_and_validate
from apps.tasks.views import COMMUNITY_KINDS
from vigil.contentyaml import ContentYamlError, slugify

_YAML_SUFFIXES = (".yaml", ".yml")
#: Keeps a directory in git and explains it. Neither is content the server can
#: list, so neither is a file with the wrong extension.
_ALLOWED_NON_YAML = (".gitkeep", "README.md")

#: A path under someone's home directory, POSIX or Windows. Anchored on a
#: boundary so ``/homeassistant/config`` is not read as a home path.
_HOME_RE = re.compile(
    r"(^|[\s\"'=])(/home/[^/\s]+/|/Users/[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)"
)

_PARSERS = {
    "tasks": parse_and_validate,
    "playbooks": parse_playbook,
    "automations": parse_automation,
}


class Command(BaseCommand):
    help = "Validate a Vigil-Approved-Scripts checkout with Vigil's own parsers."

    def add_arguments(self, parser):
        parser.add_argument("repo_dir")
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail tasks that parse with warnings (deprecated syntax).",
        )

    def handle(self, *args, **opts):
        root = Path(opts["repo_dir"])
        if not root.is_dir():
            raise CommandError(f"{root} is not a directory")

        # Per file, the lines to print for it: warnings as they come, then
        # either the reasons it failed or one ok line. Dict order is the run's
        # own — tasks, then playbooks, then automations, alphabetical inside
        # each — so CI diffs of a failing repo stay stable.
        lines: dict[str, list[str]] = {}
        # What the cross-file checks resolve references against: stem ->
        # (uid, parsed) per kind.
        index: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
            kind: {} for kind in COMMUNITY_KINDS
        }

        def fail(label: str, reason: str) -> None:
            lines.setdefault(label, []).append(f"FAIL {label}: {reason}")

        for kind in COMMUNITY_KINDS:
            directory = root / kind
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir(),
                                key=lambda item: (item.is_file(), item.name)):
                if entry.is_dir():
                    # The server lists each directory flat through the GitHub
                    # contents API, so anything below it is content nobody can
                    # see, let alone fork.
                    fail(f"{kind}/{entry.name}/",
                         "subdirectories are invisible to Vigil — "
                         "move the files up")
                    continue
                if entry.suffix not in _YAML_SUFFIXES:
                    # A file the server would list as content but cannot parse
                    # deserves a line; one that isn't content is nobody's.
                    if entry.name not in _ALLOWED_NON_YAML:
                        fail(f"{kind}/{entry.name}",
                             f"not a {', '.join(_YAML_SUFFIXES)} file")
                    continue

                label = f"{kind}/{entry.name}"
                lines[label] = []
                try:
                    text = entry.read_text(encoding="utf-8")
                    parsed = _PARSERS[kind](text)
                except (OSError, UnicodeDecodeError, SpecError,
                        ContentYamlError) as exc:
                    fail(label, str(exc))
                    continue

                for warning in parsed.get("warnings") or []:
                    if opts["strict"]:
                        fail(label, warning)
                    else:
                        lines[label].append(f"WARN {label}: {warning}")

                if kind == "tasks":
                    for home in _home_dirs(text):
                        fail(label, f"hardcoded home directory '{home}' — "
                                    "declare it as an input")
                elif (kind == "automations" and parsed["trigger"] == "event"
                        and parsed["event"] not in EVENTS):
                    # The parser accepts any event name, so one renamed years
                    # ago imports cleanly and never fires.
                    fail(label, f"unknown event '{parsed['event']}'")

                index[kind][entry.stem] = (parsed.get("uid", ""), parsed)

        _cross_check(index, fail)

        failed = sum(1 for file_lines in lines.values()
                     if any(_is_failure(line) for line in file_lines))
        for label, file_lines in lines.items():
            for line in file_lines:
                self.stdout.write(line)
            # "ok" means "nothing failed", not "nothing at all to say": a file
            # carrying a lenient WARN still passes, and CI reads the ok line as
            # the per-file verdict.
            if not any(_is_failure(line) for line in file_lines):
                self.stdout.write(f"ok   {label}")
        summary = f"{len(lines)} files, {failed} failed"
        self.stdout.write(summary)
        if failed:
            raise CommandError(f"{failed} file(s) failed validation "
                               f"({summary})")


def _is_failure(line: str) -> bool:
    return line.startswith("FAIL ")


def _cross_check(index, fail) -> None:
    """Identity rules and every reference a fork would have to resolve."""
    by_uid: dict[str, list[str]] = {}
    for kind in COMMUNITY_KINDS:
        for stem, (uid, parsed) in index[kind].items():
            label = f"{kind}/{stem}.yaml"
            wanted = slugify(parsed["name"])
            if stem != wanted:
                fail(label, f"name slugs to '{wanted}' but the file is "
                            f"'{stem}'")
            if uid:
                by_uid.setdefault(uid, []).append(label)
            else:
                fail(label, "community content must carry a uid")

    for labels in by_uid.values():
        if len(labels) < 2:
            continue
        for label in labels:
            others = ", ".join(other for other in labels if other != label)
            fail(label, f"uid is also used by {others}")

    for stem, (_, playbook) in index["playbooks"].items():
        label = f"playbooks/{stem}.yaml"
        for step in playbook["steps"]:
            target = step["task"]
            if target not in index["tasks"]:
                fail(label, f"step references task '{target}', which is not a "
                            "file in tasks/")
                continue
            step_uid = step.get("uid") or ""
            task_uid = index["tasks"][target][0]
            if step_uid and step_uid != task_uid:
                fail(label, f"step '{target}' has uid '{step_uid}' but the "
                            f"task's uid is '{task_uid}'")

    for stem, (_, automation) in index["automations"].items():
        label = f"automations/{stem}.yaml"
        action_kind = automation["action_kind"]
        kind = "playbooks" if action_kind == "playbook" else "tasks"
        target = automation["slug"]
        if target not in index[kind]:
            fail(label, f"runs {action_kind} '{target}', which is not a file "
                        f"in {kind}/")
            continue
        action_uid = automation.get("action_uid") or ""
        target_uid = index[kind][target][0]
        if action_uid and action_uid != target_uid:
            fail(label, f"action_uid '{action_uid}' does not match the "
                        f"{action_kind}'s uid '{target_uid}'")


def _home_dirs(text: str) -> list[str]:
    """Home-directory paths in a task file, branches included, inputs excepted.

    Reads the raw YAML rather than the parsed spec: ``then``/``else`` branches
    are flattened into a step list on parse, and the params a branch hides are
    exactly the ones worth naming. An input's fields are skipped because a
    declared parameter is where a machine's own path is supposed to arrive
    from — flagging it would punish the remedy the failure asks for.
    Unparseable YAML yields nothing; that was the parser's to report.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(raw, dict):
        return []
    found: list[str] = []
    _walk(raw, found)
    return found


def _walk(node: Any, found: list[str]) -> None:
    if isinstance(node, str):
        for match in _HOME_RE.finditer(node):
            home = match.group(2)
            if home not in found:
                found.append(home)
    elif isinstance(node, dict):
        for key, value in node.items():
            _walk(value, found if key != "inputs" else [])
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)
