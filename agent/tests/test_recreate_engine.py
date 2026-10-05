"""M11 03: recreate through the Engine API — the create body and the rollback."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import engine, executor
from vigil_agent.config import AgentConfig

SPEC = {
    "Name": "/web", "Image": "sha256:old",
    "Config": {"Image": "nginx:stable", "Env": ["PATH=/usr/bin", "MODE=prod"],
               "Cmd": ["nginx", "-g", "daemon off;"], "Labels": {"team": "web"},
               "ExposedPorts": {"80/tcp": {}}},
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "frontnet",
                   "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
                   "CapAdd": ["NET_ADMIN"]},
    "Mounts": [{"Type": "bind", "Source": "/srv/www", "Destination": "/usr/share/nginx/html", "RW": False},
               {"Type": "volume", "Name": "cache", "Destination": "/var/cache/nginx", "RW": True}],
}
IMAGE = {"Id": "sha256:old", "Config": {"Env": ["PATH=/usr/bin"], "Cmd": ["nginx", "-g", "daemon off;"]}}
_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")


class RecreateTests(unittest.TestCase):
    def setUp(self):
        self.create_status = 201
        self.fake = FakeEngine(routes={
            ("GET", "/containers/web/json"): (200, {**SPEC, "Image": "sha256:new"}),
            ("GET", "/images/sha256:old/json"): (200, IMAGE),
            ("DELETE", "/containers/web.vigil-old"): (204, b""),
            ("DELETE", "/containers/web"): (204, b""),
            ("POST", "/containers/web/stop"): (204, b""),
            ("POST", "/containers/web/rename"): (204, b""),
            ("POST", "/containers/web.vigil-old/rename"): (204, b""),
            ("POST", "/containers/web/start"): (204, b""),
            ("POST", "/containers/create"): lambda p, b: (self.create_status, {"Id": "new1"}
                                                          if self.create_status == 201 else {"message": "port is already allocated"}),
            ("POST", "/containers/new1/start"): (204, b""),
        })
        self.addCleanup(self.fake.close)
        client = engine.EngineClient(self.fake.path)
        for name, value in (("_engine", client),):
            p = patch.object(executor, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(executor, "_docker_inspect",
                         side_effect=lambda ref, kind="container": SPEC if kind == "container" else IMAGE)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(executor.collector, "request_docker_recheck")
        p.start()
        self.addCleanup(p.stop)

    def _calls(self):
        return [(m, path.split("?")[0].split("/", 2)[2], body) for m, path, body in self.fake.requests
                if path.startswith("/v1.")]

    def test_create_body_carries_only_what_the_user_set(self):
        out = executor._recreate_container({"container_name": "web", "image": "nginx:1.27"}, _CFG)
        create = next(b for m, p, b in self._calls() if p == "containers/create")
        self.assertEqual(create["Image"], "nginx:1.27")
        self.assertEqual(create["Env"], ["MODE=prod"], "the image's own env is not frozen in")
        self.assertNotIn("Cmd", create, "the image's own command still applies")
        self.assertEqual(create["Labels"], {"team": "web"})
        hc = create["HostConfig"]
        self.assertEqual(hc["Binds"], ["/srv/www:/usr/share/nginx/html:ro", "cache:/var/cache/nginx"])
        self.assertEqual(hc["PortBindings"], {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]})
        self.assertEqual((hc["NetworkMode"], hc["CapAdd"]), ("frontnet", ["NET_ADMIN"]))
        self.assertEqual(hc["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(out.data["old_image_id"], "sha256:old")
        self.assertTrue(out.data["updated"])
        order = [p for m, p, b in self._calls() if m == "POST"]
        self.assertEqual(order[:4], ["containers/web/stop", "containers/web/rename",
                                     "containers/create", "containers/new1/start"])

    def test_a_failed_create_puts_the_original_back(self):
        self.create_status = 409
        with self.assertRaises(RuntimeError) as ctx:
            executor._recreate_container({"container_name": "web"}, _CFG)
        self.assertIn("original container restored", str(ctx.exception))
        self.assertIn("port is already allocated", str(ctx.exception))
        posts = [p for m, p, b in self._calls() if m == "POST"]
        self.assertEqual(posts[-2:], ["containers/web.vigil-old/rename", "containers/web/start"])

    def test_compose_containers_are_refused(self):
        spec = {**SPEC, "Config": {**SPEC["Config"], "Labels": {"com.docker.compose.project": "s"}}}
        with patch.object(executor, "_docker_inspect", return_value=spec):
            with self.assertRaises(ValueError):
                executor._recreate_container({"container_name": "web"}, _CFG)


if __name__ == "__main__":
    unittest.main()
