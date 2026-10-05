"""M11 14: private registry pulls carry X-Registry-Auth from the server."""
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import client, engine, executor
from vigil_agent.actions import containers
from vigil_agent.config import AgentConfig

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")


class RegistryOfTests(unittest.TestCase):
    def test_reads_like_the_engine(self):
        cases = {"nginx:stable": "docker.io", "library/nginx": "docker.io",
                 "ghcr.io/acme/app:1": "ghcr.io", "registry.local:5000/app": "registry.local:5000",
                 "localhost/app": "localhost", "acme/app@sha256:" + "a" * 64: "docker.io"}
        for image, registry in cases.items():
            with self.subTest(image=image):
                self.assertEqual(containers.registry_of(image), registry)


class AuthHeaderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEngine(routes={("POST", "/images/create"): (200, b'{"status":"Downloaded"}\n')})
        self.addCleanup(self.fake.close)
        p = patch.object(executor, "_engine", return_value=engine.EngineClient(self.fake.path))
        p.start()
        self.addCleanup(p.stop)
        p = patch("vigil_agent.config.load_config", return_value=_CFG)
        p.start()
        self.addCleanup(p.stop)

    def test_a_held_login_rides_the_pull(self):
        with patch.object(client, "fetch_registry_auth",
                          return_value={"username": "bot", "password": "s3cret", "serveraddress": "ghcr.io"}) as fetch:
            executor._pull("ghcr.io/acme/app:1")
        fetch.assert_called_once_with(_CFG, "ghcr.io")
        header = self.fake.headers[-1].get("X-Registry-Auth")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header)),
                         {"username": "bot", "password": "s3cret", "serveraddress": "ghcr.io"})

    def test_no_login_pulls_anonymously(self):
        with patch.object(client, "fetch_registry_auth", return_value=None):
            executor._pull("nginx:stable")
        self.assertNotIn("X-Registry-Auth", self.fake.headers[-1])


if __name__ == "__main__":
    unittest.main()
