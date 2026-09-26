"""Script results ship their per-step {id, status, result} to the server.

`report_result` adds the `steps` key only when one is given (old callers
and the pre-execution abort path send nothing), and
`_execute_script_task` passes the runtime's StepResults through.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import __main__ as agent_main
from vigil_agent import client, executor
from vigil_agent.config import AgentConfig


def _config(tmp: str) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="full_control",
        data_dir=Path(tmp),
    )


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class ReportResultStepsTests(unittest.TestCase):
    def test_report_result_sends_steps(self):
        steps = [{"id": "svc", "status": "ok", "result": {"active": True}}]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(client.requests, "post") as mock_post,
        ):
            mock_post.return_value.json.return_value = {}
            client.report_result(_config(tmp), "t1", "completed", "out", steps=steps)
        _, kwargs = mock_post.call_args
        self.assertIn("steps", kwargs["json"])
        self.assertEqual(kwargs["json"]["steps"], steps)


class ExecuteScriptTaskStepsTests(unittest.TestCase):
    def _run(self, params, task, patched_executor=None):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(client, "report_result") as mock_report,
        ):
            if patched_executor is not None:
                patched_executor.__enter__()
            try:
                agent_main._execute_script_task("t1", params, _config(tmp), task)
            finally:
                if patched_executor is not None:
                    patched_executor.__exit__(None, None, None)
        return mock_report

    def test_completed_report_carries_steps(self):
        params = {
            "steps": [
                {
                    "id": "svc",
                    "action": "check_service",
                    "params": {"service_name": "nginx"},
                },
            ],
            "variables": {},
        }
        task = {"id": "t1"}
        mock_report = self._run(
            params, task,
            patched_executor=patch.object(
                executor.subprocess, "run", return_value=_FakeCompleted("active\n")
            ),
        )

        mock_report.assert_called_once()
        _, kwargs = mock_report.call_args
        self.assertEqual(
            kwargs.get("steps"),
            [
                {
                    "id": "svc",
                    "status": "ok",
                    "result": {"active": True, "state": "active"},
                }
            ],
        )

    def test_hunt_steps_carry_their_matches(self):
        params = {
            "steps": [
                {"id": "svc", "action": "check_service",
                 "params": {"service_name": "nginx"}},
                {"id": "jars", "action": "hunt_file",
                 "params": {"name": "log4j-core-2.14.1.jar", "paths": "/tmp"}},
            ],
            "variables": {},
        }
        task = {"id": "t1"}

        hunt_body = {"matches": [{"evidence_type": "file",
                                  "path": "/tmp/log4j-core-2.14.1.jar",
                                  "size": 3}],
                    "truncated": False, "duration": 0.12, "timed_out": False}

        def fake_execute(action, p, cfg, **kw):
            if action == "check_service":
                return executor.ActionOutput(
                    "active\n", {"active": True, "state": "active"})
            if action == "hunt_file":
                return executor.ActionOutput(
                    json.dumps(hunt_body, sort_keys=True),
                    {"matched": True, "count": 1, "truncated": False})
            raise ValueError(f"unexpected action {action!r}")

        mock_report = self._run(params, task,
                                patched_executor=patch.object(
                                    executor, "execute_action",
                                    side_effect=fake_execute))

        mock_report.assert_called_once()
        _, kwargs = mock_report.call_args
        steps = kwargs.get("steps")
        self.assertEqual(len(steps), 2)
        svc, jars = steps
        self.assertNotIn("hunt", svc)
        self.assertEqual(jars["id"], "jars")
        self.assertEqual(jars["status"], "ok")
        self.assertEqual(jars["hunt"], {
            "matches": hunt_body["matches"],
            "truncated": False,
            "duration": 0.12,
            "timed_out": False,
        })

    def test_hunt_step_with_unparseable_output_omits_hunt(self):
        params = {
            "steps": [
                {"id": "jars", "action": "hunt_file",
                 "params": {"name": "*.jar"}},
            ],
            "variables": {},
        }
        task = {"id": "t1"}

        mock_report = self._run(
            params, task,
            patched_executor=patch.object(
                executor, "execute_action",
                return_value=executor.ActionOutput("not json", {"matched": False})),
        )

        mock_report.assert_called_once()
        _, kwargs = mock_report.call_args
        self.assertEqual(kwargs.get("steps"),
                         [{"id": "jars", "status": "ok",
                           "result": {"matched": False}}])


if __name__ == "__main__":
    unittest.main()
