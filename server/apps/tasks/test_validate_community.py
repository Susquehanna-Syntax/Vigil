"""manage.py validate_community — the community repo's content, by Vigil's parsers.

The repo's CI runs this against a checked-out Vigil. These tests build a repo
in a temp directory and run the command over it, so what they pin is the
command's contract with that CI: which files pass, which fail, and what a
failure names so a contributor can find the file and the rule.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

_TASK_UID = "04954106-4ce1-48d7-a232-408624b49405"
_PLAYBOOK_UID = "29d90af5-69fc-5c7b-a4d2-734741abb534"
_AUTOMATION_UID = "62bac7e3-7ae6-5e5b-bcd3-01348338d99f"


def _task(name: str = "Restart a service",
          marker: str = "${{ inputs.service }}", uid: str = _TASK_UID) -> str:
    return f"""name: {name}
uid: {uid}
author: Connor Haggerty
description: >-
  Restarts one systemd service and proves it came back.
risk: standard
inputs:
  - id: service
    type: text
    label: Service
    required: true
actions:
  - id: restart
    type: restart_service
    params:
      service_name: "{marker}"
"""


def _playbook(task: str = "restart-a-service", uid: str = _TASK_UID) -> str:
    return f"""name: Keep the web tier healthy
uid: {_PLAYBOOK_UID}
author: Connor Haggerty
description: >-
  Restarts the web service on a host that reported a problem.
steps:
  - task: {task}
    uid: {uid}
    order: 1
"""


def _automation(**overrides: str) -> str:
    fields = {
        "name": "Restart nginx when it alerts",
        "uid": _AUTOMATION_UID,
        "author": "Connor Haggerty",
        "description": "Runs the web tier playbook on a host that alerted.",
        "trigger": "schedule",
        "cron": None,
        "action_kind": "playbook",
        "playbook": "keep-the-web-tier-healthy",
        "action_uid": _PLAYBOOK_UID,
        "target": "all",
    }
    fields.update(overrides)
    lines = [f"{key}: {value}" for key, value in fields.items()
             if value is not None]
    if fields["cron"] is None and fields["trigger"] == "schedule":
        lines.insert(lines.index("trigger: schedule") + 1,
                     "cron: {minute: \"0\", hour: \"3\", dom: \"*\", "
                     "month: \"*\", dow: \"*\"}")
    return "\n".join(lines) + "\n"


class _Repo:
    """A minimal Vigil-Approved-Scripts checkout in a temp directory."""

    def __init__(self, root: Path):
        self.root = root
        for kind in ("tasks", "playbooks", "automations"):
            (root / kind).mkdir(parents=True)
            (root / kind / ".gitkeep").write_text("", encoding="utf-8")
        self.write("tasks/restart-a-service.yaml", _task())
        self.write("playbooks/keep-the-web-tier-healthy.yaml", _playbook())
        self.write("automations/restart-nginx-when-it-alerts.yaml", _automation())

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _run(root: Path, *extra: str) -> str:
    buf = StringIO()
    call_command("validate_community", str(root), *extra, stdout=buf)
    return buf.getvalue()


def _fail_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("FAIL ")]


class ValidateCommunityTests(TestCase):
    def setUp(self):
        cleaner = tempfile.TemporaryDirectory()
        self.addCleanup(cleaner.cleanup)
        self.repo = _Repo(Path(cleaner.name))

    def _strict(self) -> str:
        return _run(self.repo.root, "--strict")

    def _fails(self, *extra: str) -> str:
        buf = StringIO()
        with self.assertRaises(CommandError):
            call_command("validate_community", str(self.repo.root), *extra,
                         stdout=buf)
        return buf.getvalue()

    def test_a_valid_repo_passes(self):
        out = self._strict()
        self.assertEqual(
            [line for line in out.splitlines() if line.startswith(("ok", "WARN"))],
            ["ok   tasks/restart-a-service.yaml",
             "ok   playbooks/keep-the-web-tier-healthy.yaml",
             "ok   automations/restart-nginx-when-it-alerts.yaml"], out)
        self.assertEqual("3 files, 0 failed", out.strip().splitlines()[-1])

    def test_a_deprecated_field_fails_strict_and_warns_leniently(self):
        """Free-text ``relevance:`` is the other deprecation ``--strict`` is
        for, alongside the input-marker one."""
        self.repo.write("tasks/restart-a-service.yaml",
                        _task().replace("risk: standard",
                                        "risk: standard\n"
                                        "relevance: web servers"))
        strict = self._fails("--strict")
        self.assertEqual(
            [("FAIL tasks/restart-a-service.yaml: relevance: is free text and "
              "is now part of the description \u2014 use relevant: to decide "
              "where a task applies")],
            _fail_lines(strict), strict)

        lenient = _run(self.repo.root)
        self.assertEqual(
            [("WARN tasks/restart-a-service.yaml: relevance: is free text and "
              "is now part of the description \u2014 use relevant: to decide "
              "where a task applies"),
             "ok   tasks/restart-a-service.yaml",
             "ok   playbooks/keep-the-web-tier-healthy.yaml",
             "ok   automations/restart-nginx-when-it-alerts.yaml",
             "3 files, 0 failed"],
            lenient.splitlines(), lenient)

    def test_a_failing_file_is_counted_once_however_many_rules_it_breaks(self):
        """Wrong-named *and* uid-clashing: two FAIL lines, one entry in the
        count the CI reads."""
        self.repo.write("tasks/foo.yaml", _task())
        out = self._fails()
        self.assertEqual(
            [("FAIL tasks/foo.yaml: name slugs to 'restart-a-service' "
              "but the file is 'foo'"),
             ("FAIL tasks/foo.yaml: uid is also used by "
              "tasks/restart-a-service.yaml"),
             ("FAIL tasks/restart-a-service.yaml: uid is also used by "
              "tasks/foo.yaml")],
            [line for line in out.splitlines() if line.startswith("FAIL ")],
            out)
        self.assertEqual("4 files, 2 failed", out.strip().splitlines()[-1])

    def test_old_inputs_marker_fails_strict_but_not_lenient(self):
        self.repo.write("tasks/restart-a-service.yaml",
                        _task(marker="{{ inputs.service }}"))
        strict = self._fails("--strict")
        self.assertEqual(
            [("FAIL tasks/restart-a-service.yaml: action #1 param "
              "'service_name': {{ inputs.service }} is the old input syntax "
              "\u2014 write ${{ inputs.service }}")],
            _fail_lines(strict), strict)

        lenient = _run(self.repo.root)
        self.assertEqual(
            [("WARN tasks/restart-a-service.yaml: action #1 param "
              "'service_name': {{ inputs.service }} is the old input syntax "
              "\u2014 write ${{ inputs.service }}"),
             "ok   tasks/restart-a-service.yaml",
             "ok   playbooks/keep-the-web-tier-healthy.yaml",
             "ok   automations/restart-nginx-when-it-alerts.yaml",
             "3 files, 0 failed"],
            [line for line in lenient.splitlines()
             if line.startswith(("WARN", "ok", "3 files"))], lenient)

    def test_baseline_automation_is_rejected(self):
        self.repo.write("automations/restart-nginx-when-it-alerts.yaml",
                        _automation(action_kind="baseline"))
        out = self._fails("--strict")
        self.assertIn("FAIL automations/restart-nginx-when-it-alerts.yaml", out)
        self.assertIn("'action_kind' must be 'task' or 'playbook'", out)

    def test_missing_reference(self):
        self.repo.write("playbooks/keep-the-web-tier-healthy.yaml",
                        _playbook(task="nope"))
        out = self._fails("--strict")
        self.assertIn("FAIL playbooks/keep-the-web-tier-healthy.yaml", out)
        self.assertIn("task 'nope'", out)

    def test_uid_mismatch(self):
        self.repo.write("playbooks/keep-the-web-tier-healthy.yaml",
                        _playbook(uid=_PLAYBOOK_UID))
        out = self._fails("--strict")
        self.assertIn("FAIL playbooks/keep-the-web-tier-healthy.yaml", out)
        self.assertIn("but the task's uid is", out)

    def test_stem_must_match_name(self):
        self.repo.root.joinpath("tasks/restart-a-service.yaml").unlink()
        self.repo.write("tasks/foo.yaml", _task())
        out = self._fails("--strict")
        self.assertIn("FAIL tasks/foo.yaml", out)
        self.assertIn("name slugs to 'restart-a-service' but the file is 'foo'",
                      out)

    def test_duplicate_uid(self):
        self.repo.write("tasks/keep-nginx-running.yaml",
                        _task().replace("name: Restart a service",
                                        "name: Keep nginx running"))
        out = self._fails("--strict")
        self.assertIn("FAIL tasks/keep-nginx-running.yaml", out)
        self.assertIn("uid is also used by tasks/restart-a-service.yaml", out)
        self.assertIn("FAIL tasks/restart-a-service.yaml", out)

    def test_missing_uid_is_refused(self):
        self.repo.write("tasks/restart-a-service.yaml", _task(uid=""))
        out = self._fails("--strict")
        self.assertIn("tasks/restart-a-service.yaml: community content must "
                      "carry a uid", out)

    def test_subdirectory_is_refused(self):
        self.repo.write("playbooks/linux/x.yaml", _playbook())
        out = self._fails("--strict")
        self.assertIn("FAIL playbooks/linux/:", out)
        self.assertIn("invisible", out)

    def test_home_directory_is_refused(self):
        self.repo.write(
            "tasks/restart-a-service.yaml",
            _task() + "  - id: seed\n"
                      "    type: write_file\n"
                      "    params:\n"
                      "      path: /home/alice/.config/x.service\n"
                      "      content: placeholder\n")
        out = self._fails("--strict")
        self.assertIn("FAIL tasks/restart-a-service.yaml: hardcoded home "
                      "directory '/home/alice/'", out)

    def test_a_home_directory_hidden_in_a_branch_is_still_refused(self):
        self.repo.write("tasks/restart-a-service.yaml",
                        _task() + "  - if: agent.os == 'linux'\n"
                                  "    then:\n"
                                  "      - id: seed\n"
                                  "        type: write_file\n"
                                  "        params:\n"
                                  "          path: \"C:\\\\Users\\\\bob\\\\x\"\n"
                                  "          content: placeholder\n")
        out = self._fails("--strict")
        self.assertIn("hardcoded home directory 'C:\\Users\\bob\\'", out)

    def test_an_input_is_where_a_machine_path_belongs(self):
        """The failure tells the author to declare the path as an input, so a
        declared default naming one is the fix, not a second offence."""
        self.repo.write(
            "tasks/restart-a-service.yaml",
            _task().replace("    required: true",
                            "    required: true\n"
                            "    default: /home/alice/.cache/app.log"))
        out = self._strict()
        self.assertIn("ok   tasks/restart-a-service.yaml", out)

    def test_unknown_event_is_refused(self):
        self.repo.write("automations/restart-nginx-when-it-alerts.yaml",
                        _automation(trigger="event", event="alert_fired",
                                    cron=None))
        out = self._fails("--strict")
        self.assertIn("unknown event 'alert_fired'", out)

    def test_action_uid_must_match_the_target(self):
        self.repo.write("automations/restart-nginx-when-it-alerts.yaml",
                        _automation(action_uid=_TASK_UID))
        out = self._fails("--strict")
        self.assertIn("action_uid", out)
        self.assertIn("does not match the playbook's uid", out)

    def test_a_deprecated_file_is_not_also_ok_when_it_breaks_another_rule(self):
        """The WARN line is lenient; the file still fails on the reference, and
        must not then print a contradicting ok line."""
        self.repo.write("playbooks/keep-the-web-tier-healthy.yaml",
                        _playbook(task="nope") + "")
        self.repo.write("tasks/restart-a-service.yaml",
                        _task(marker="{{ inputs.service }}"))
        out = self._fails()
        self.assertIn("WARN tasks/restart-a-service.yaml", out)
        self.assertIn("FAIL playbooks/keep-the-web-tier-healthy.yaml", out)
        self.assertEqual(2, out.count("tasks/restart-a-service.yaml"), out)
        self.assertNotIn("ok   playbooks/keep-the-web-tier-healthy.yaml", out)

    def test_not_a_directory(self):
        with self.assertRaises(CommandError) as caught:
            call_command("validate_community",
                         str(self.repo.root / "nope"), stdout=StringIO())
        self.assertIn("is not a directory", str(caught.exception))
