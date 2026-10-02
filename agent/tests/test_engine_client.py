"""M11 phase 01: the Engine API client — discovery, negotiation, identity."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import DOCKER_VERSION, PODMAN_VERSION, FakeEngine
from vigil_agent import engine


class EngineClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEngine(routes={("GET", "/containers/json"): (200, [{"Id": "abc"}]),
                                       ("POST", "/containers/abc/restart"): (204, b""),
                                       ("POST", "/containers/zzz/restart"): (404, {"message": "No such container: zzz"})})
        self.addCleanup(self.fake.close)
        self.client = engine.EngineClient(self.fake.path)

    def test_negotiates_down_to_what_the_agent_speaks(self):
        self.assertTrue(self.client.ping())
        self.assertEqual(self.client.api_version(), engine.MAX_API)
        self.assertEqual(self.client.get("/containers/json", query={"all": 1}), [{"Id": "abc"}])
        self.assertIn(("GET", f"/v{engine.MAX_API}/containers/json?all=1", None), self.fake.requests)

    def test_post_and_errors(self):
        self.assertIsNone(self.client.post("/containers/abc/restart", query={"t": 10}))
        with self.assertRaises(engine.EngineError) as ctx:
            self.client.post("/containers/zzz/restart")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("No such container: zzz", str(ctx.exception))

    def test_describe_docker_and_podman(self):
        self.assertEqual(self.client.describe(), {"kind": "docker", "version": "27.3.1",
                                                  "api_version": engine.MAX_API, "rootless": False})
        podman = FakeEngine(version=PODMAN_VERSION)
        self.addCleanup(podman.close)
        info = engine.EngineClient(podman.path, rootless=True, uid=1000).describe()
        self.assertEqual(info, {"kind": "podman", "version": "5.2.2", "api_version": "1.41",
                                "rootless": True, "uid": 1000})

    def test_a_dead_socket_is_an_engine_error(self):
        dead = engine.EngineClient("/nonexistent/engine.sock")
        self.assertFalse(dead.ping())
        with self.assertRaises(engine.EngineError):
            dead.get("/containers/json", versioned=False)


class ApiFloorTests(unittest.TestCase):
    def test_respects_the_engines_minimum(self):
        fake = FakeEngine(version={**DOCKER_VERSION, "ApiVersion": "1.52",
                                   "MinAPIVersion": "1.48"})
        self.addCleanup(fake.close)
        fake.routes[("GET", "/containers/json")] = (200, [{"Id": "abc"}])
        client = engine.EngineClient(fake.path)
        self.assertEqual(client.api_version(), "1.48")
        self.assertEqual(client.get("/containers/json"), [{"Id": "abc"}])
        self.assertIn(("GET", "/v1.48/containers/json", None), fake.requests)

    def test_docker_29_speaks_max_api(self):
        fake = FakeEngine(version={**DOCKER_VERSION, "ApiVersion": "1.52",
                                   "MinAPIVersion": "1.44"})
        self.addCleanup(fake.close)
        self.assertEqual(engine.MAX_API, "1.47")
        self.assertEqual(engine.EngineClient(fake.path).api_version(), engine.MAX_API)

    def test_old_engine_without_minimum(self):
        fake = FakeEngine(version={**PODMAN_VERSION, "ApiVersion": "1.40"})
        self.addCleanup(fake.close)
        self.assertEqual(engine.EngineClient(fake.path).api_version(), "1.40")


class DiscoveryTests(unittest.TestCase):
    def test_order_and_rootless_uid(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ("var/run/docker.sock", "run/podman/podman.sock",
                        "run/user/1000/podman/podman.sock", "srv/custom.sock"):
                p = Path(root, rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            found = engine.discover_sockets({"DOCKER_HOST": "unix:///srv/custom.sock"}, root=root)
            self.assertEqual([os.path.relpath(f["path"], root) for f in found],
                             ["srv/custom.sock", "var/run/docker.sock", "run/podman/podman.sock",
                              "run/user/1000/podman/podman.sock"])
            self.assertEqual((found[-1]["rootless"], found[-1]["uid"]), (True, 1000))
            tcp = engine.discover_sockets({"DOCKER_HOST": "tcp://10.0.0.5:2375"}, root=root)
            self.assertNotIn("10.0.0.5", " ".join(f["path"] for f in tcp))

    def test_report_is_none_without_an_engine(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(engine.engine_report({}, root=root))


if __name__ == "__main__":
    unittest.main()
