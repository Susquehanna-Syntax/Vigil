"""M11 06: stack_restart / stack_update find the compose files from the labels."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import engine, executor
from vigil_agent.config import AgentConfig, _ALL_ACTIONS

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")
LABELS = {"com.docker.compose.project": "media",
          "com.docker.compose.project.config_files": "/opt/media/compose.yaml,/opt/media/extra.yaml",
          "com.docker.compose.project.working_dir": "/opt/media"}


class StackActionTests(unittest.TestCase):
    def setUp(self):
        self.containers = [{"Id": "a", "Labels": LABELS}]
        self.fake = FakeEngine(routes={("GET", "/containers/json"): lambda p, b: (200, self.containers)})
        self.addCleanup(self.fake.close)
        self.calls = []
        for name, value in (("_engine", engine.EngineClient(self.fake.path)),
                            ("_compose_cmd", ["docker", "compose"])):
            p = patch.object(executor, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(executor, "_run", side_effect=lambda cmd, timeout=60, extra_env=None:
                         self.calls.append((cmd, extra_env)) or "ok")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor, "_validate_path", side_effect=lambda v, label: Path(v))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor.collector, "request_docker_recheck")
        p.start()
        self.addCleanup(p.stop)

    def test_restart_uses_the_labels(self):
        out = executor._stack_restart({"project": "media"}, _CFG)
        cmd, env = self.calls[0]
        self.assertEqual(cmd, ["docker", "compose", "-p", "media", "--project-directory", "/opt/media",
                               "-f", "/opt/media/compose.yaml", "-f", "/opt/media/extra.yaml", "restart"])
        self.assertEqual(env, {"DOCKER_HOST": f"unix://{self.fake.path}"})
        self.assertEqual(out.data, {"project": "media"})
        query = self.fake.requests[-1][1]
        self.assertIn("filters=", query)
        self.assertIn("com.docker.compose.project%3Dmedia", query)

    def test_update_pulls_then_ups(self):
        executor._stack_update({"project": "media"}, _CFG)
        self.assertEqual([c[0][-2:] for c in self.calls], [["/opt/media/extra.yaml", "pull"],
                                                            ["up", "-d"]])

    def test_refusals(self):
        self.containers = [{"Id": "a", "Labels": {"com.docker.compose.project": "media"}}]
        with self.assertRaises(ValueError):
            executor._stack_restart({"project": "media"}, _CFG)
        with self.assertRaises(ValueError):
            executor._stack_restart({"project": "-p evil"}, _CFG)
        self.assertEqual(self.calls, [])

    def test_registered(self):
        for action in ("stack_restart", "stack_update"):
            self.assertIn(action, _ALL_ACTIONS)
            self.assertIn(action, executor._HANDLERS)


if __name__ == "__main__":
    unittest.main()
