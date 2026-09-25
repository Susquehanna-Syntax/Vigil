"""Script results ship their per-step {id, status, result} to the server.

`report_result` adds the `steps` key only when one is given (old callers
and the pre-execution abort path send nothing), and
`_execute_script_task` passes the runtime's StepResults through.
"""

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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                executor.subprocess, "run", return_value=_FakeCompleted("active\n")
            ),
            patch.object(client, "report_result") as mock_report,
        ):
            agent_main._execute_script_task("t1", params, _config(tmp), task)

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


if __name__ == "__main__":
    unittest.main()
