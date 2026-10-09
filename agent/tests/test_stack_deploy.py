"""M11 09: stack_deploy writes the stack, fetches the .env once, ups it."""
import dataclasses
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import client, executor
from vigil_agent.actions import stacks
from vigil_agent.config import AgentConfig, _ALL_ACTIONS

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")
TICKET = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
COMPOSE = "services:\n  web:\n    image: nginx:stable\n    env_file: .env\n"


class StackDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "stacks"
        self.calls = []
        for target, attr, value in ((executor, "_compose_cmd", ["docker", "compose"]),
                                    (executor, "_compose_env", {"DOCKER_HOST": "unix:///x.sock"})):
            p = patch.object(target, attr, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(executor, "_run", side_effect=lambda cmd, timeout=60, extra_env=None:
                         self.calls.append(cmd) or "up")
        p.start()
        self.addCleanup(p.stop)
        for target, attr in ((executor.collector, "request_docker_recheck"),):
            p = patch.object(target, attr)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(stacks, "STACKS_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _params(self, **extra):
        return {"project": "media", "compose": COMPOSE, "working_dir": str(self.root / "media"),
                "env_ticket": TICKET, "revision": 3, **extra}

    def test_deploy_writes_files_and_fetches_env_once(self):
        with patch.object(client, "fetch_stack_env", return_value="API_KEY=hunter2\n") as fetch, \
                patch.object(stacks, "_safe_workdir"), \
                patch.object(stacks, "_check_resolved", return_value={"services": {}}):   # test_compose_check.py
            cfg = dataclasses.replace(_CFG, data_dir=self.root / "agent-data")
            out = stacks._stack_deploy(self._params(), cfg)
        fetch.assert_called_once_with(cfg, TICKET)
        workdir = self.root / "media"
        self.assertEqual((workdir / "compose.yaml").read_text(), COMPOSE)
        env = workdir / ".env"
        self.assertEqual(env.read_text(), "API_KEY=hunter2\n")
        self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o600)
        up = self.calls[0]
        self.assertEqual(up[:5], ["docker", "compose", "-p", "media", "--project-directory"])
        self.assertEqual(up[5], str(workdir))
        self.assertEqual(up[-3:], ["up", "-d", "--remove-orphans"])
        # up reads the checked configuration, not compose.yaml again: kept in the agent's
        # data dir (outside the stack folder, owner-only) for the containers' labels to name
        checked = self.root / "agent-data" / "stacks" / "media.json"
        self.assertEqual(up[6:8], ["-f", str(checked)])
        self.assertEqual(json.loads(checked.read_text()), {"services": {}})
        self.assertEqual(stat.S_IMODE(os.stat(checked).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(checked.parent).st_mode), 0o700)
        self.assertEqual(out.data, {"project": "media", "revision": 3})
        self.assertNotIn("hunter2", str(out))

    def test_a_refused_ticket_starts_nothing(self):
        with patch.object(client, "fetch_stack_env", side_effect=RuntimeError("used or expired")):
            with self.assertRaises(RuntimeError):
                stacks._stack_deploy(self._params(), _CFG)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "media").exists())

    def test_param_refusals(self):
        for bad in ({"project": "Media"}, {"working_dir": "relative/dir"},
                    {"working_dir": "/opt/../etc"}, {"env_ticket": "x; rm -rf /"}, {"compose": " "}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                stacks._stack_deploy(self._params(**bad), _CFG)
        self.assertEqual(self.calls, [])

    def test_remove_downs_and_only_deletes_vigils_own_folder(self):
        workdir = self.root / "media"
        workdir.mkdir(parents=True)
        (workdir / "compose.yaml").write_text(COMPOSE)
        checked = self.root / "agent-data" / "stacks" / "media.json"
        checked.parent.mkdir(parents=True)
        checked.write_text("{}")
        out = stacks._stack_remove({"project": "media", "working_dir": str(workdir), "delete_files": True},
                                   dataclasses.replace(_CFG, data_dir=self.root / "agent-data"))
        self.assertFalse(checked.exists(), "the checked configuration goes with the stack")
        self.assertEqual(self.calls[0][-1], "down")
        self.assertEqual(out.data, {"project": "media", "files_deleted": True})
        self.assertFalse(workdir.exists())
        self.calls.clear()
        with self.assertRaises(ValueError):
            stacks._stack_remove({"project": "media", "working_dir": "/srv/media",
                                  "delete_files": True}, _CFG)
        self.assertEqual(self.calls, [], "refused before compose ran")

    def test_registered(self):
        for action in ("stack_deploy", "stack_remove"):
            self.assertIn(action, _ALL_ACTIONS)
            self.assertIn(action, executor._HANDLERS)


if __name__ == "__main__":
    unittest.main()
