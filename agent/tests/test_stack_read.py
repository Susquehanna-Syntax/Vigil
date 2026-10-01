"""M11 12: stack_read — adopt a stack in place, secrets off the task result."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import client, engine, executor
from vigil_agent.actions import stacks
from vigil_agent.config import AgentConfig, _ALL_ACTIONS

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")
TICKET = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class StackReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n")
        (self.dir / ".env").write_text("DB_PASSWORD=hunter2\n")
        labels = lambda svc, h: {"com.docker.compose.project": "shop",  # noqa: E731
                                 "com.docker.compose.service": svc,
                                 "com.docker.compose.config-hash": h,
                                 "com.docker.compose.project.config_files": str(self.dir / "docker-compose.yml"),
                                 "com.docker.compose.project.working_dir": str(self.dir)}
        self.fake = FakeEngine(routes={("GET", "/containers/json"): (200, [
            {"Id": "a", "Labels": labels("web", "h-web")}, {"Id": "b", "Labels": labels("db", "h-old")}])})
        self.addCleanup(self.fake.close)
        for name, value in (("_engine", engine.EngineClient(self.fake.path)),
                            ("_compose_cmd", ["docker", "compose"]), ("_compose_env", {})):
            p = patch.object(executor, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(executor, "_run", return_value="web h-web\ndb h-new\n")
        self.run_mock = p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor, "_validate_path", side_effect=lambda v, label: Path(v))
        p.start()
        self.addCleanup(p.stop)

    def test_reads_posts_and_keeps_secrets_out_of_the_result(self):
        with patch.object(client, "post_stack_read") as post:
            out = stacks._stack_read({"project": "shop", "adopt_ticket": TICKET}, _CFG)
        payload = post.call_args.args[2]
        self.assertEqual(post.call_args.args[1], TICKET)
        self.assertEqual((payload["compose_file"], payload["working_dir"]),
                         ("docker-compose.yml", str(self.dir)))
        self.assertEqual(payload["env"], "DB_PASSWORD=hunter2\n")
        self.assertEqual(payload["hashes"], {"match": ["web"], "recreate": ["db"]})
        self.assertNotIn("hunter2", str(out))
        self.assertEqual(out.data, {"project": "shop", "would_recreate": 1})
        self.assertEqual(self.run_mock.call_args.args[0][-3:], ["config", "--hash", "*"])

    def test_refusals(self):
        with patch.object(client, "post_stack_read") as post:
            for params in ({"project": "shop", "adopt_ticket": "nope"}, {"project": "Shop", "adopt_ticket": TICKET}):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    stacks._stack_read(params, _CFG)
        post.assert_not_called()

    def test_deploy_writes_an_adopted_stacks_own_file(self):
        with patch.object(client, "fetch_stack_env", return_value=""), \
                patch.object(executor.collector, "request_docker_recheck"):
            stacks._stack_deploy({"project": "shop", "compose": "services:\n  web:\n    image: nginx\n",
                                  "working_dir": str(self.dir), "compose_file": "docker-compose.yml"}, _CFG)
        self.assertIn(str(self.dir / "docker-compose.yml"), self.run_mock.call_args.args[0])
        with self.assertRaises(ValueError):
            stacks._stack_deploy({"project": "shop", "compose": "services: {}", "working_dir": str(self.dir),
                                  "compose_file": "../etc/x.yml"}, _CFG)

    def test_registered(self):
        self.assertIn("stack_read", _ALL_ACTIONS)
        self.assertIs(executor._HANDLERS["stack_read"], stacks._stack_read)


if __name__ == "__main__":
    unittest.main()
