"""QA-07: compose paths written by a manager that runs compose in its own
container (Portainer) are the container's, not the host's — map them through
the mount that holds them before compose or Vigil reads them."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import engine, executor
from vigil_agent.actions import containers
from vigil_agent.config import AgentConfig

from tests.fake_engine import FakeEngine

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="full_control")
PORTAINER_COMPOSE = "/data/compose/49/docker-compose.yml"
PORTAINER_ENV = "/data/compose/49/stack.env"
PORTAINER_WD = "/data/compose/49"


def _mount(source: str, destination: str) -> dict:
    return {"Type": "bind", "Source": source, "Destination": destination}


class HostPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(executor, "_validate_path", side_effect=lambda v, label: Path(v))
        p.start()
        self.addCleanup(p.stop)

    def _engine(self, containers_):
        fake = FakeEngine(routes={("GET", "/containers/json"): (200, containers_)})
        self.addCleanup(fake.close)
        p = patch.object(executor, "_engine", return_value=engine.EngineClient(fake.path))
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_existing_path_is_unchanged(self):
        existing = self.root / "docker-compose.yml"
        existing.write_text("services: {}\n")
        fake = self._engine([{"Id": "p", "Mounts": [_mount(str(self.root / "host"), "/data")]}])
        self.assertEqual(containers._host_path(str(existing)), str(existing))
        self.assertFalse([r for r in fake.requests if "/containers/json" in r[1]])

    def test_maps_through_the_containers_mount(self):
        host = self.root / "host" / "compose" / "49"
        host.mkdir(parents=True)
        compose = host / "docker-compose.yml"
        compose.write_text("services:\n  searxng:\n    image: searxng/searxng\n")
        self._engine([{"Id": "p", "Mounts": [_mount(str(self.root / "host"), "/data"),
                                             _mount("/var/run/docker.sock", "/var/run/docker.sock")]}])
        self.assertEqual(containers._host_path(PORTAINER_COMPOSE), str(compose))

    def test_longest_destination_wins(self):
        a = self.root / "a" / "compose" / "49"
        b = self.root / "b" / "49"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (b / "docker-compose.yml").write_text("services: {}\n")
        self._engine([{"Id": "p", "Mounts": [_mount(str(self.root / "b"), "/data/compose"),
                                             _mount(str(self.root / "a"), "/data")]}])
        self.assertEqual(containers._host_path(PORTAINER_COMPOSE), str(b / "docker-compose.yml"))

    def test_unexplained_path_is_unchanged(self):
        self._engine([{"Id": "p", "Mounts": [_mount(str(self.root / "elsewhere"), "/srv")]}])
        self.assertEqual(containers._host_path(PORTAINER_COMPOSE), PORTAINER_COMPOSE)

    def test_a_mount_that_maps_to_nothing_keeps_the_label_path(self):
        self._engine([{"Id": "p", "Mounts": [_mount(str(self.root / "host"), "/data")]}])
        self.assertEqual(containers._host_path(PORTAINER_COMPOSE), PORTAINER_COMPOSE)

    def test_engine_error_is_unchanged(self):
        """An agent with no answering engine keeps the label path — the
        caller's error then names it. _mount_lookup swallows nothing else."""
        from vigil_agent.engine import EngineError
        for boom in (RuntimeError("No container engine found (Docker or Podman socket)"),
                     EngineError("engine refused", status=500),
                     OSError("socket gone"),
                     ValueError("not json")):
            p = patch.object(executor, "_engine", side_effect=boom)
            p.start()
            self.addCleanup(p.stop)
            self.assertEqual(containers._host_path(PORTAINER_COMPOSE), PORTAINER_COMPOSE)

    def test_the_labelled_path_is_what_an_unexplained_failure_names(self):
        """The fallback of last resort is the label itself, so an admin who
        hits it sees the path Portainer's own stack entry shows."""
        self._engine([{"Id": "p", "Mounts": []}])
        self.assertEqual(containers._host_path("/data/compose/49/.env"),
                         "/data/compose/49/.env")
        self.assertEqual(containers._label_env_files(
            {"com.docker.compose.project.environment_file": "/data/compose/49/stack.env"}), [])


class PortainerActionTests(unittest.TestCase):
    """The actions the cphmserv report hit, end to end through the seams: the
    argv that reaches compose carries host paths, never a container's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = Path(self.tmp.name) / "portainer_data"
        stack = self.host / "compose" / "49"
        stack.mkdir(parents=True)
        (stack / "docker-compose.yml").write_text("services:\n  searxng:\n    image: searxng/searxng\n")
        (stack / "stack.env").write_text("SEARXNG_SECRET=abc\n")
        self.compose = stack / "docker-compose.yml"
        self.env = stack / "stack.env"
        self.fake = FakeEngine(routes={("GET", "/containers/json"): (200, [
            {"Id": "p", "Mounts": [_mount(str(self.host), "/data"),
                                    _mount("/var/run/docker.sock", "/var/run/docker.sock")]}])})
        self.addCleanup(self.fake.close)
        self.calls = []
        for name, value in (("_engine", engine.EngineClient(self.fake.path)),
                            ("_compose_cmd", ["docker", "compose"]),
                            ("_compose_env", {})):
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

    def test_portainer_container_rollback(self):
        spec = {"Config": {
            "Image": "searxng/searxng:latest",
            "Labels": {"com.docker.compose.project": "searxng",
                       "com.docker.compose.service": "searxng",
                       "com.docker.compose.project.config_files": PORTAINER_COMPOSE,
                       "com.docker.compose.project.working_dir": PORTAINER_WD,
                       "com.docker.compose.project.environment_file": PORTAINER_ENV}}}
        with patch.object(executor, "_docker_inspect", return_value=spec):
            executor._container_rollback({"container_name": "searxng",
                                          "image": "searxng/searxng:2025-09-17"}, _CFG)

        cmd = self.calls[0]
        override = str(self.host / "compose" / "49" / containers.ROLLBACK_OVERRIDE)
        self.assertEqual(
            cmd,
            ["docker", "compose", "-p", "searxng", "--project-directory",
             str(self.host / "compose" / "49"), "--env-file", str(self.env),
             "-f", str(self.compose), "-f", override, "up", "-d", "--no-deps", "searxng"])
        self.assertFalse([a for a in cmd if a.startswith("/data/")],
                         f"a container path leaked into the host's argv: {cmd!r}")
        self.assertEqual((self.host / "compose" / "49" / containers.ROLLBACK_OVERRIDE).read_text(),
                         "# Written by Vigil: searxng rolled back. The next update removes it.\n"
                         "services:\n  searxng:\n    image: \"searxng/searxng:2025-09-17\"\n")

    def test_portainer_stack_compose_args(self):
        self.fake.routes[("GET", "/containers/json")] = (200, [{
            "Id": "s",
            "Labels": {"com.docker.compose.project": "searxng",
                       "com.docker.compose.project.config_files": PORTAINER_COMPOSE,
                       "com.docker.compose.project.working_dir": PORTAINER_WD,
                       "com.docker.compose.project.environment_file": PORTAINER_ENV},
            "Mounts": [_mount(str(self.host), "/data")]}])
        self.assertEqual(
            containers._stack_compose_args("searxng"),
            ["-p", "searxng", "--project-directory", str(self.host / "compose" / "49"),
             "--env-file", str(self.env), "-f", str(self.compose)])

    def test_portainer_update_container(self):
        spec = {"Image": "sha256:old", "Config": {
            "Image": "searxng/searxng:latest",
            "Labels": {"com.docker.compose.project": "searxng",
                       "com.docker.compose.service": "searxng",
                       "com.docker.compose.project.config_files": PORTAINER_COMPOSE,
                       "com.docker.compose.project.working_dir": PORTAINER_WD,
                       "com.docker.compose.project.environment_file": PORTAINER_ENV}}}
        with patch.object(executor, "_docker_inspect", return_value=spec), \
                patch.object(executor, "_pull", return_value=""), \
                patch.object(executor, "_image_id", return_value="sha256:new"), \
                patch.object(executor, "_container_image_id", return_value="sha256:new"):
            executor._update_container({"container_name": "searxng"}, _CFG)

        self.assertEqual(len(self.calls), 2)
        tail = ["--project-directory", str(self.host / "compose" / "49"),
                "--env-file", str(self.env), "-f", str(self.compose)]
        self.assertEqual(self.calls[0], ["docker", "compose", *tail, "pull", "searxng"])
        self.assertEqual(self.calls[1], ["docker", "compose", *tail,
                                         "up", "-d", "--no-deps", "searxng"])
        for cmd in self.calls:   # both compose runs carry the host's argv
            self.assertFalse([a for a in cmd if a.startswith("/data/")],
                             f"a container path leaked into the host's argv: {cmd!r}")
        # compose writes the labels from the paths it is handed, so the same
        # re-up has to carry the env file — otherwise it interpolates blanks
        # and recreates every service of the stack.
        self.assertEqual(tail.count("--env-file"), 1)
        self.assertEqual(tail[tail.index("--env-file") + 1], str(self.env))


if __name__ == "__main__":
    unittest.main()
