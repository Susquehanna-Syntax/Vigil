"""Service and container actions report the fact a later step branches on."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t", mode="full_control")
    base.update(kw)
    return AgentConfig(**base)


def _fake_run(answers):
    def run(cmd, timeout=None, extra_env=None):
        for key, value in answers.items():
            if key in " ".join(cmd):
                return value
        return "ok"
    return run


def _systemctl(answer):
    return patch.object(executor.subprocess, "run",
                        return_value=SimpleNamespace(stdout=answer + "\n", returncode=0))


class ServiceOutputTests(unittest.TestCase):
    def test_service_actions_report_active(self):
        for handler in (executor._restart_service, executor._start_service,
                        executor._stop_service, executor._reload_service):
            with patch.object(executor, "_run", _fake_run({})):
                with _systemctl("active"):
                    self.assertEqual(handler({"service_name": "nginx"}, _config()).data, {"active": True})
                with _systemctl("inactive"):
                    self.assertEqual(handler({"service_name": "nginx"}, _config()).data, {"active": False})

    def test_enable_disable_report_enabled(self):
        with patch.object(executor, "_run", _fake_run({})), _systemctl("enabled"):
            self.assertEqual(executor._enable_service({"service_name": "nginx"}, _config()).data, {"enabled": True})
        with patch.object(executor, "_run", _fake_run({})), _systemctl("disabled"):
            self.assertEqual(executor._disable_service({"service_name": "nginx"}, _config()).data, {"enabled": False})


class ContainerOutputTests(unittest.TestCase):
    """Driven through a fake engine on a real Unix socket (M11)."""

    def setUp(self):
        from tests.fake_engine import FakeEngine
        from vigil_agent import engine

        patcher = patch.object(executor.collector, "request_docker_recheck")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.running = True
        self.fake = FakeEngine(routes={
            ("POST", "/containers/web/restart"): (204, b""),
            ("POST", "/containers/web/start"): (304, b""),
            ("POST", "/containers/web/stop"): (204, b""),
            ("DELETE", "/containers/web"): (204, b""),
            ("GET", "/containers/web/json"): lambda path, body: (
                200, {"State": {"Running": self.running}, "LogPath": self.log_path}),
            ("POST", "/images/create"): (200, b'{"status":"Pulling from library/nginx"}\n'
                                              b'{"status":"Status: Image is up to date"}\n'),
            ("GET", "/images/nginx:alpine/json"): (200, {"Id": "sha256:abc"}),
        })
        self.addCleanup(self.fake.close)
        self.log_path = "/nonexistent/x.log"
        client = engine.EngineClient(self.fake.path)
        engine_patch = patch.object(executor, "_engine", return_value=client)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

    def test_container_actions_report_running(self):
        for handler, running in ((executor._restart_container, True),
                                 (executor._start_container, True),
                                 (executor._stop_container, False)):
            self.running = running
            self.assertEqual(handler({"container_name": "web"}, _config()).data,
                             {"running": running})

    def test_pull_reports_image_id(self):
        out = executor._pull_image({"image": "nginx:alpine"}, _config())
        self.assertEqual(out.data, {"image_id": "sha256:abc"})
        self.assertIn("up to date", out)
        self.assertIn(("POST", f"/v1.43/images/create?fromImage=nginx&tag=alpine", None),
                      self.fake.requests)

    def test_a_pull_error_in_the_stream_fails(self):
        self.fake.routes[("POST", "/images/create")] = (
            200, b'{"error":"pull access denied for nope"}\n')
        with self.assertRaises(RuntimeError) as ctx:
            executor._pull_image({"image": "nope:1"}, _config())
        self.assertIn("pull access denied", str(ctx.exception))

    def test_remove_reports_removed(self):
        self.assertEqual(executor._remove_container({"container_name": "web"}, _config()).data,
                         {"removed": True})
        self.assertIn(("DELETE", "/v1.43/containers/web?force=true", None), self.fake.requests)

    def test_compose_reports_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".yml") as f:
            with patch.object(executor, "_run", _fake_run({})) as run, \
                 patch.object(executor, "_compose_cmd", return_value=["docker", "compose"]), \
                 patch.object(executor, "_validate_path", return_value=Path(f.name)):
                self.assertEqual(executor._docker_compose_up({"compose_file": f.name}, _config()).data,
                                 {"compose_file": f.name})
                self.assertEqual(executor._docker_compose_down({"compose_file": f.name}, _config()).data,
                                 {"compose_file": f.name})

    def test_compose_env_points_at_the_engine_socket(self):
        self.assertEqual(executor._compose_env(), {"DOCKER_HOST": f"unix://{self.fake.path}"})

    def test_clear_logs_reports_truncated(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"log")
        self.log_path = f.name
        self.assertEqual(executor._clear_docker_logs({"container_name": "web"}, _config()).data,
                         {"truncated": True})
        self.log_path = "/nonexistent/x.log"
        self.assertEqual(executor._clear_docker_logs({"container_name": "web"}, _config()).data,
                         {"truncated": False})
        self.assertEqual(executor._clear_docker_logs({}, _config()).data, {"truncated": False})


if __name__ == "__main__":
    unittest.main()
