"""M11 13: roll a container back — an override pin for compose, a recreate
otherwise — and the next update clears the pin."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import executor
from vigil_agent.config import AgentConfig, _ALL_ACTIONS

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")
DIGEST = "jellyfin/jellyfin@sha256:" + "d" * 64


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "compose.yaml").write_text("services:\n  jellyfin:\n    image: jellyfin/jellyfin\n")
        self.labels = {"com.docker.compose.project": "media", "com.docker.compose.service": "jellyfin",
                       "com.docker.compose.project.working_dir": str(self.dir),
                       "com.docker.compose.project.config_files": str(self.dir / "compose.yaml")}
        self.calls = []
        for name, value in (("_compose_cmd", ["docker", "compose"]), ("_compose_env", {})):
            p = patch.object(executor, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(executor, "_run", side_effect=lambda cmd, timeout=60, extra_env=None:
                         self.calls.append(cmd) or "ok")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor, "_validate_path", side_effect=lambda v, label: Path(v))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor.collector, "request_docker_recheck")
        p.start()
        self.addCleanup(p.stop)

    def _inspect(self, labels):
        return patch.object(executor, "_docker_inspect", return_value={"Config": {"Labels": labels}})

    def test_compose_rollback_pins_with_an_override_and_leaves_the_file(self):
        before = (self.dir / "compose.yaml").read_text()
        with self._inspect(self.labels):
            out = executor._container_rollback({"container_name": "jellyfin", "image": DIGEST}, _CFG)
        override = self.dir / executor.ROLLBACK_OVERRIDE
        self.assertIn(DIGEST, override.read_text())
        self.assertEqual((self.dir / "compose.yaml").read_text(), before)
        self.assertEqual(self.calls[0][-6:], ["-f", str(override), "up", "-d", "--no-deps", "jellyfin"])
        self.assertEqual(out.data, {"rolled_back": True, "image": DIGEST})

    def test_standalone_rollback_recreates_on_the_image(self):
        with self._inspect({}), patch.object(executor, "_recreate_container") as recreate:
            executor._container_rollback({"container_name": "web", "image": "sha256:" + "a" * 64}, _CFG)
        recreate.assert_called_once_with({"container_name": "web", "image": "sha256:" + "a" * 64}, _CFG)

    def test_the_next_update_clears_the_pin(self):
        override = self.dir / executor.ROLLBACK_OVERRIDE
        override.write_text("services: {}\n")
        labels = {**self.labels, "com.docker.compose.project.config_files":
                  f"{self.dir / 'compose.yaml'},{override}"}
        spec = {"Image": "sha256:old", "Config": {"Image": "jellyfin/jellyfin:latest", "Labels": labels}}
        with patch.object(executor, "_docker_inspect", return_value=spec), \
                patch.object(executor, "_container_image_id", return_value="sha256:new"):
            executor._update_container({"container_name": "jellyfin"}, _CFG)
        self.assertFalse(override.exists())
        self.assertFalse(any(str(override) in " ".join(c) for c in self.calls),
                         "the update must not bring the pin back")

    def test_refusals(self):
        with self._inspect(self.labels):
            for params in ({"container_name": "jellyfin", "image": "-evil"},
                           {"container_name": "-x", "image": DIGEST}):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    executor._container_rollback(params, _CFG)
        self.assertEqual(self.calls, [])

    def test_registered(self):
        self.assertIn("container_rollback", _ALL_ACTIONS)
        self.assertIn("container_rollback", executor._HANDLERS)


if __name__ == "__main__":
    unittest.main()
