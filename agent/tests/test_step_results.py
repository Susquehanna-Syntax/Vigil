"""Steps record their declared outputs, and later steps can read them.

`ActionOutput` is a str subclass carrying a `.data` dict of the declared
outputs. The runtime stores every top-level step as
`ctx["steps"][<name>] = {"status": ..., "result": ...}`, so
`${{ steps.<id>.result.<field> }}` resolves through the existing templater
with no templater change.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig
from vigil_agent.runtime import TaskRuntime, resolve_value


def _config(tmp: str) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="full_control",
        data_dir=Path(tmp),
    )


def _spec(image_id, image_ref, labels=None):
    return {
        "Image": image_id,
        "Config": {
            "Image": image_ref,
            "Labels": labels or {},
        },
    }


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class ActionOutputTests(unittest.TestCase):
    def test_action_output_is_still_a_string(self):
        out = executor.ActionOutput("x", {"a": 1})
        self.assertEqual(out, "x")
        self.assertIsInstance(out, str)
        self.assertEqual(out.data, {"a": 1})

    def test_action_output_defaults_to_empty_data(self):
        out = executor.ActionOutput("x")
        self.assertEqual(out.data, {})


class CheckServiceOutputTests(unittest.TestCase):
    def test_check_service_reports_active(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                executor.subprocess, "run", return_value=_FakeCompleted("active\n")
            ),
        ):
            out = executor._check_service({"service_name": "nginx"}, _config(tmp))
        self.assertEqual(out.data, {"active": True, "state": "active"})
        self.assertIn("nginx", out)


class UpdateContainerOutputTests(unittest.TestCase):
    def test_update_container_reports_ids(self):
        def fake_run(cmd, timeout=60):
            if cmd[:2] == ["docker", "inspect"]:
                return "sha256:newimageidnew"
            return "ok"

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(executor, "_run", fake_run),
            patch.object(
                executor,
                "_docker_inspect",
                return_value=_spec("sha256:oldoldoldoldold", "searxng/searxng:latest"),
            ),
            patch.object(executor, "_recreate_container", return_value="recreated"),
        ):
            out = executor._update_container({"container_name": "web"}, _config(tmp))

        self.assertEqual(
            out.data,
            {
                "updated": True,
                "old_image_id": "sha256:oldoldoldoldold",
                "new_image_id": "sha256:newimageidnew",
            },
        )


class StepResultResolutionTests(unittest.TestCase):
    """The runtime records steps, and a later step's params can read them.

    ``check_service`` runs against a patched ``subprocess.run`` (the real
    handler), and the second step's ``run_command`` goes through a patched
    ``executor._run`` that records argv and returns "ok".
    """

    def _run_two_step(self, echo_param):
        calls = []

        def fake_run(cmd, timeout=60, extra_env=None):
            calls.append(list(cmd))
            return "ok"

        def fake_subprocess_run(cmd, *a, **k):
            return _FakeCompleted("active\n")

        payload = {
            "steps": [
                {
                    "name": "svc",
                    "action": "check_service",
                    "params": {"service_name": "nginx"},
                },
                {
                    "name": "echo",
                    "action": "run_command",
                    "params": {"command": echo_param},
                },
            ],
            "variables": {},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(executor.subprocess, "run", side_effect=fake_subprocess_run),
            patch.object(executor, "_run", fake_run),
        ):
            results = TaskRuntime(payload, _config(tmp)).run()
        return results, calls

    def test_later_step_reads_earlier_result(self):
        results, calls = self._run_two_step("echo ${{ steps.svc.result.state }}")
        self.assertEqual([r.state for r in results], ["ok", "ok"])
        self.assertEqual(calls, [["echo", "active"]])

    def test_status_is_visible_to_later_steps(self):
        results, calls = self._run_two_step("echo ${{ steps.svc.status }}")
        self.assertEqual([r.state for r in results], ["ok", "ok"])
        self.assertEqual(calls, [["echo", "ok"]])

    def test_failed_step_has_no_data(self):
        payload = {
            "steps": [
                {
                    "name": "svc",
                    "action": "check_service",
                    "params": {"service_name": "nginx"},
                },
            ],
            "variables": {},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(executor.subprocess, "run", side_effect=RuntimeError("boom")),
        ):
            results = TaskRuntime(payload, _config(tmp)).run()

        self.assertEqual(results[0].state, "error")
        self.assertEqual(results[0].data, {})


class TemplaterStepRefTests(unittest.TestCase):
    def test_exact_ref_keeps_its_type(self):
        ctx = {"steps": {"svc": {"result": {"active": True}}}}
        self.assertIs(resolve_value("${{ steps.svc.result.active }}", ctx), True)


if __name__ == "__main__":
    unittest.main()
