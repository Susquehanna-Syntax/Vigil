"""update_container: one action that updates a container from a name alone.

The old UI-side fix (``_staticDockerFix``) guessed all three things wrong:
it used the container's image *ID* where an image *reference* is needed, it
guessed the compose path as ``/opt/<stack>/docker-compose.yml``, and it
offered the update as an AI suggestion when no model is needed at all.
This action reads all three facts from the container itself instead.

``_run`` and ``_docker_inspect`` are mocked — no docker ever runs here.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig

COMPOSE_LABELS = {
    "com.docker.compose.project": "stack",
    "com.docker.compose.service": "web",
    "com.docker.compose.project.config_files": "/srv/stack/docker-compose.yml",
}


def _config(tmp: str) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="full_control",
        data_dir=Path(tmp),
    )


def _spec(image_id, image_ref, labels=None):
    return {
        "Image": image_id,
        "Config": {
            "Image": image_ref,
            "Labels": labels or {},
        },
    }


class UpdateContainerTests(unittest.TestCase):
    """Both container shapes, driven entirely by the container's own labels
    and Config.Image — the server supplies nothing but the name."""

    def _update(self, spec, inspect_after="sha256:newimageidnew"):
        calls = []

        def fake_run(cmd, timeout=60):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return inspect_after
            return "ok"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(executor, "_run", fake_run), \
                patch.object(executor, "_docker_inspect", return_value=spec):
            out = executor._update_container({"container_name": "web"}, _config(tmp))
        return out, calls

    def test_a_compose_container_updates_through_compose(self):
        out, calls = self._update(
            _spec("sha256:oldoldoldoldold", "searxng/searxng:latest", COMPOSE_LABELS))

        pull = [c for c in calls if "compose" in c and "pull" in c]
        self.assertEqual(pull, [[
            "docker", "compose", "-f", "/srv/stack/docker-compose.yml",
            "pull", "web"]])
        up = [c for c in calls if "compose" in c and "up" in c]
        self.assertEqual(up, [[
            "docker", "compose", "-f", "/srv/stack/docker-compose.yml",
            "up", "-d", "--no-deps", "web"]])
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in calls))
        self.assertIn("via compose", out)

    def test_the_compose_file_comes_from_the_label_not_a_guess(self):
        labels = dict(COMPOSE_LABELS)
        labels["com.docker.compose.project.config_files"] = \
            "/srv/custom/compose.yaml,/srv/custom/override.yaml"
        _out, calls = self._update(
            _spec("sha256:oldoldoldoldold", "nginx:stable", labels))

        for cmd in calls:
            self.assertFalse(
                any("/opt/" in arg for arg in cmd),
                f"guessed /opt/ path leaked into {cmd!r}")
        self.assertIn("/srv/custom/compose.yaml",
                      " ".join(" ".join(c) for c in calls))
        # The first listed file wins; the override is not our concern.
        self.assertFalse(any("override.yaml" in arg for c in calls for arg in c))

    def test_a_standalone_container_pulls_then_recreates(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(executor, "_run") as run, \
                patch.object(executor, "_docker_inspect",
                             return_value=_spec("sha256:oldoldoldoldold",
                                                "searxng/searxng:latest")), \
                patch.object(executor, "_recreate_container",
                             return_value="recreated") as recreate:
            run.side_effect = lambda cmd, timeout=60: "sha256:newimageidnew"
            executor._update_container({"container_name": "web"}, _config(tmp))

        pulls = [c.args[0] for c in run.call_args_list
                 if c.args[0][:2] == ["docker", "pull"]]
        self.assertEqual(pulls, [["docker", "pull", "searxng/searxng:latest"]])
        recreate.assert_called_once_with(
            {"container_name": "web", "image": "searxng/searxng:latest"},
            _config(tmp))

    def test_a_digest_pinned_container_is_left_alone(self):
        out, calls = self._update(
            _spec("sha256:oldoldoldoldold", "nginx@sha256:" + "a" * 64))

        self.assertIn("pinned", out)
        self.assertFalse(
            any(any(a in ("pull", "up", "run") for a in c) for c in calls),
            f"a pinned container must not be touched: {calls!r}")

    def test_the_image_reference_is_used_not_the_image_id(self):
        _out, calls = self._update(
            _spec("sha256:deadbeefdeadbeefdeadbeef", "searxng/searxng:latest"),
            inspect_after="sha256:newimageidnew")

        pull = [c for c in calls if c[:2] == ["docker", "pull"]]
        self.assertEqual(pull, [["docker", "pull", "searxng/searxng:latest"]])
        self.assertFalse(
            any(arg.startswith("sha256:") for c in calls for arg in c),
            f"an image ID leaked into an argv: {calls!r}")

    def test_missing_compose_labels_raise_a_useful_error(self):
        labels = dict(COMPOSE_LABELS)
        del labels["com.docker.compose.project.config_files"]
        spec = _spec("sha256:oldoldoldoldold", "nginx:stable", labels)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(executor, "_run"), \
                patch.object(executor, "_docker_inspect", return_value=spec), \
                self.assertRaises(ValueError) as ctx:
            executor._update_container({"container_name": "web"}, _config(tmp))

        message = str(ctx.exception)
        self.assertIn("web", message)
        self.assertIn("docker_compose_up", message)

    def test_an_unchanged_image_says_already_current(self):
        out, _ = self._update(
            _spec("sha256:sameimagesamesam", "searxng/searxng:latest"),
            inspect_after="sha256:sameimagesamesam")
        self.assertIn("already current", out)
        self.assertNotIn("image updated", out)


if __name__ == "__main__":
    unittest.main()
