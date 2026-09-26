"""`when:` is evaluated per step, at the moment the step is reached.

M4 phase 03 moved evaluation out of `_execute_script_task`, which used to
pre-evaluate every predicate against a context that had no steps in it at
all — so `steps.svc.result.active == True` could never be true. The runtime
now evaluates each step's `when:` just before running it, with the results
of the earlier steps in context; a false predicate records the step as
`skipped` (visible later as `steps.<id>.status == "skipped"`) and the task
goes on. `__main__` keeps a parse-only check so a predicate that cannot
even be parsed aborts the whole task before any step runs.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import sys
import tempfile
import unittest
from pathlib import Path
from platform import system
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import __main__, executor
from vigil_agent.config import AgentConfig


def _config(tmp: str) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="full_control",
        data_dir=Path(tmp),
    )


class _FakeOutput:
    """An execute_action output with declared data (steps.<id>.result.*)."""

    def __init__(self, text: str, data: dict):
        self.text = text
        self.data = data


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class _StopScriptTask(Exception):
    """Unwind _execute_script_task once the report is captured."""


def _run_script(steps, reports, commands):
    """Drive _execute_script_task end to end; return the reports captured.

    `check_service` runs against a patched subprocess.run (systemctl says
    "active") and every run_command goes through a patched executor._run
    that records argv and returns "ok".
    """

    def fake_run(cmd, timeout=60, extra_env=None):
        commands.append(list(cmd))
        return "ok"

    def fake_subprocess_run(cmd, *a, **k):
        return _FakeCompleted("active\n")

    task_id = "t-when"
    params = {"action": "run_script", "steps": steps, "variables": {"variables": {}}}
    task = {"task_id": task_id, "action": "run_script"}

    def _capture(reports, name):
        def rec(cfg, _task, output, steps=None):
            reports.append((name, output))
            raise _StopScriptTask()

        return rec

    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(executor.subprocess, "run", side_effect=fake_subprocess_run),
        patch.object(executor, "_run", fake_run),
        patch.object(
            __main__,
            "_report_completed",
            side_effect=_capture(reports, "_report_completed"),
        ),
        patch.object(
            __main__, "_report_failed", side_effect=_capture(reports, "_report_failed")
        ),
        patch.object(
            __main__,
            "_report_skipped",
            side_effect=_capture(reports, "_report_skipped"),
        ),
    ):
        try:
            __main__._execute_script_task(task_id, params, _config(tmp), task)
        except _StopScriptTask:
            pass


class WhenPerStepTests(unittest.TestCase):
    def _steps(self, *specs):
        return [
            {
                "id": sid,
                "action": action,
                "params": params,
                **({"when": when} if when else {}),
            }
            for sid, action, params, when in specs
        ]

    def test_when_reads_an_earlier_result(self):
        reports, commands = [], []
        _run_script(
            self._steps(
                ("svc", "check_service", {"service_name": "nginx"}, None),
                (
                    "restart",
                    "run_command",
                    {"command": "echo restart"},
                    "steps.svc.result.active == True",
                ),
            ),
            reports,
            commands,
        )
        self.assertEqual(commands, [["echo", "restart"]])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[OK] restart", output)

    def test_false_when_skips_and_continues(self):
        reports, commands = [], []
        _run_script(
            self._steps(
                ("svc", "check_service", {"service_name": "nginx"}, None),
                (
                    "restart",
                    "run_command",
                    {"command": "echo restart"},
                    "steps.svc.result.active == False",
                ),
                ("final", "run_command", {"command": "echo final"}, None),
            ),
            reports,
            commands,
        )
        self.assertEqual(commands, [["echo", "final"]])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[SKIPPED] restart", output)
        self.assertIn("[OK] final", output)

    def test_skipped_status_is_visible_later(self):
        reports, commands = [], []
        _run_script(
            self._steps(
                ("svc", "check_service", {"service_name": "nginx"}, None),
                (
                    "restart",
                    "run_command",
                    {"command": "echo restart"},
                    "steps.svc.result.active == False",
                ),
                (
                    "after",
                    "run_command",
                    {"command": "echo after"},
                    'steps.restart.status == "skipped"',
                ),
            ),
            reports,
            commands,
        )
        self.assertEqual(commands, [["echo", "after"]])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[OK] after", output)

    def test_agent_context_still_works(self):
        # The real context (built in _build_when_context) maps agent.os
        # through platform.system().lower(), not platform.system() verbatim.
        os_name = system().lower()
        reports, commands = [], []
        _run_script(
            self._steps(
                (
                    "svc",
                    "check_service",
                    {"service_name": "nginx"},
                    f'agent.os == "{os_name}"',
                ),
            ),
            reports,
            commands,
        )
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[OK] svc", output)

    def test_unparseable_when_aborts_before_any_step(self):
        reports, commands = [], []
        _run_script(
            self._steps(
                ("svc", "check_service", {"service_name": "nginx"}, "foo("),
                ("final", "run_command", {"command": "echo final"}, None),
            ),
            reports,
            commands,
        )
        self.assertEqual(commands, [])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_failed")
        self.assertIn("aborted before execution", output)

    def test_all_skipped_reports_skipped(self):
        reports, commands = [], []
        _run_script(
            self._steps(
                (
                    "a",
                    "run_command",
                    {"command": "echo a"},
                    'agent.os == "definitely-not-a-real-os"',
                ),
                ("b", "run_command", {"command": "echo b"}, 'steps.a.status == "ok"'),
            ),
            reports,
            commands,
        )
        self.assertEqual(commands, [])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_skipped")
        self.assertIn("[SKIPPED] a", output)
        self.assertIn("[SKIPPED] b", output)


    def _capture(self, reports, name):
        def rec(cfg, _task, output, steps=None):
            reports.append((name, output))
            raise _StopScriptTask()

        return rec

    def test_number_comparison_reaches_the_agent(self):
        # The spec's branching example: step 2 runs only when step 1's
        # declared count output is greater than zero.
        reports, commands = [], []
        _run_script(
            self._steps(
                ("one", "run_command", {"command": "echo one"}, None),
                (
                    "two",
                    "run_command",
                    {"command": "echo two"},
                    "steps.one.result.count > 0",
                ),
            ),
            reports,
            commands,
        )
        # run_command declares exit_code but no count, so the comparison is
        # over a missing (None) value: False, and step 2 is skipped.
        self.assertEqual(commands, [["echo", "one"]])
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[OK] one", output)
        self.assertIn("[SKIPPED] two", output)

        # With a real count in step 1's result the same predicate runs
        # step 2. Step 1's declared data carries the count field the
        # predicate reads.
        reports, commands = [], []

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                executor,
                "execute_action",
                side_effect=[
                    _FakeOutput("step one", {"count": 2}),
                    _FakeOutput("step two", {}),
                ],
            ),
            patch.object(
                __main__,
                "_report_completed",
                side_effect=self._capture(reports, "_report_completed"),
            ),
            patch.object(
                __main__, "_report_failed", side_effect=self._capture(reports, "_report_failed")
            ),
            patch.object(
                __main__,
                "_report_skipped",
                side_effect=self._capture(reports, "_report_skipped"),
            ),
        ):
            task_id = "t-count"
            steps = [
                {"id": "one", "action": "check_service",
                 "params": {"service_name": "nginx"}},
                {"id": "two", "action": "run_command",
                 "params": {"command": "echo two"},
                 "when": "steps.one.result.count > 0"},
            ]
            try:
                __main__._execute_script_task(
                    task_id,
                    {"action": "run_script", "steps": steps,
                     "variables": {"variables": {}}},
                    _config(tmp),
                    {"task_id": task_id, "action": "run_script"},
                )
            except _StopScriptTask:
                pass
        self.assertEqual(len(reports), 1)
        kind, output = reports[0]
        self.assertEqual(kind, "_report_completed")
        self.assertIn("[OK] one", output)
        self.assertIn("[OK] two", output)


if __name__ == "__main__":
    unittest.main()
