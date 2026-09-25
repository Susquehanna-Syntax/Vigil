"""Service and container actions report the fact a later step branches on."""
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
    def setUp(self):
        patcher = patch.object(executor.collector, "request_docker_recheck")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_container_actions_report_running(self):
        for handler, running in ((executor._restart_container, "true"),
                                 (executor._start_container, "true"),
                                 (executor._stop_container, "false")):
            with patch.object(executor, "_run", _fake_run({"State.Running": running})):
                self.assertEqual(handler({"container_name": "web"}, _config()).data,
                                 {"running": running == "true"})

    def test_pull_reports_image_id(self):
        with patch.object(executor, "_run", _fake_run({"image inspect": "sha256:abc\n"})):
            self.assertEqual(executor._pull_image({"image": "nginx:alpine"}, _config()).data,
                             {"image_id": "sha256:abc"})

    def test_remove_reports_removed(self):
        with patch.object(executor, "_run", _fake_run({})):
            self.assertEqual(executor._remove_container({"container_name": "web"}, _config()).data,
                             {"removed": True})

    def test_compose_reports_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".yml") as f:
            with patch.object(executor, "_run", _fake_run({})), \
                 patch.object(executor, "_validate_path", return_value=Path(f.name)):
                self.assertEqual(executor._docker_compose_up({"compose_file": f.name}, _config()).data,
                                 {"compose_file": f.name})
                self.assertEqual(executor._docker_compose_down({"compose_file": f.name}, _config()).data,
                                 {"compose_file": f.name})

    def test_clear_logs_reports_truncated(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"log")
        with patch.object(executor, "_run", _fake_run({"LogPath": f.name})):
            self.assertEqual(executor._clear_docker_logs({"container_name": "web"}, _config()).data,
                             {"truncated": True})
        with patch.object(executor, "_run", _fake_run({"LogPath": "/nonexistent/x.log"})):
            self.assertEqual(executor._clear_docker_logs({"container_name": "web"}, _config()).data,
                             {"truncated": False})
        self.assertEqual(executor._clear_docker_logs({}, _config()).data, {"truncated": False})


if __name__ == "__main__":
    unittest.main()
