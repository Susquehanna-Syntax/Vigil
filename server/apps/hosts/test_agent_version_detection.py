"""The expected agent version is detected, not configured.

It used to be ``os.environ.get("VIGIL_AGENT_VERSION", <literal>)``, which meant
three copies of the number — the literal in settings, the default in
docker-compose.yml, and whatever sat in each operator's .env — all of which had
to be bumped by hand every release. They drifted immediately: compose said
2026.3.8, settings said 2026.3.16, the wiki said 2026.2.2. Because the shipped
compose file always passed the variable, its stale default won everywhere.

check_outdated_agents compares every host against this value, so getting it
wrong either alerts a whole fleet that is perfectly current or stays silent
while every host runs an agent too old to do its job.
"""
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from vigil import settings as vigil_settings


def _agent_version_assignment() -> str:
    """Name of whatever produces settings.VIGIL_AGENT_VERSION.

    Read out of the syntax tree rather than by grepping the file: settings.py
    deliberately quotes the old ``os.environ.get(...)`` expression and the
    versions that drifted in its explanatory docstring, and a text search
    cannot tell that prose from the code it describes.
    """
    import ast

    tree = ast.parse(Path(vigil_settings.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "VIGIL_AGENT_VERSION" not in names:
            continue
        value = node.value
        if isinstance(value, ast.Call):
            # _detect_agent_version() or os.environ.get(...)
            func = value.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute):
                return f"{ast.unparse(func.value)}.{func.attr}"
        return type(value).__name__
    raise AssertionError("settings.py no longer assigns VIGIL_AGENT_VERSION")


class DetectionOrderTests(TestCase):
    """Most specific source wins: stamped file, then agent source, then the
    server's own version."""

    def _detect(self, dist_dir, base_dir):
        with patch.object(vigil_settings, "VIGIL_AGENT_DIST_DIR", Path(dist_dir)), \
             patch.object(vigil_settings, "BASE_DIR", Path(base_dir)):
            return vigil_settings._detect_agent_version()

    def _make_source_tree(self, root, version):
        """A checkout where server/ and agent/ sit side by side."""
        agent = Path(root) / "agent" / "vigil_agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "__version__.py").write_text(f'__version__ = "{version}"\n')
        server = Path(root) / "server"
        server.mkdir(parents=True, exist_ok=True)
        return server

    def test_the_stamped_version_file_wins(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "agent_dist"
            dist.mkdir()
            (dist / "VERSION").write_text("2026.6.2\n")
            server = self._make_source_tree(tmp, "1900.1.1")
            self.assertEqual(self._detect(dist, server), "2026.6.2")

    def test_trailing_whitespace_is_stripped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "agent_dist"
            dist.mkdir()
            (dist / "VERSION").write_text("  2026.6.2  \n\n")
            self.assertEqual(self._detect(dist, Path(tmp) / "server"), "2026.6.2")

    def test_falls_back_to_the_agent_source(self):
        """No stamped file — a source checkout, which is the repo layout."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            server = self._make_source_tree(tmp, "2026.6.2")
            self.assertEqual(
                self._detect(Path(tmp) / "nonexistent", server), "2026.6.2")

    def test_falls_back_to_the_server_version_when_nothing_is_present(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            got = self._detect(Path(tmp) / "no-dist", Path(tmp) / "no-server")
            self.assertEqual(got, vigil_settings.VIGIL_VERSION)

    def test_an_empty_stamp_does_not_win(self):
        """A zero-byte VERSION must not pin the fleet to the empty string —
        check_outdated_agents skips entirely on a falsy value."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "agent_dist"
            dist.mkdir()
            (dist / "VERSION").write_text("   \n")
            server = self._make_source_tree(tmp, "2026.6.2")
            self.assertEqual(self._detect(dist, server), "2026.6.2")

    def test_detection_never_raises_on_an_unreadable_stamp(self):
        """A version string is not worth failing startup over."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "agent_dist"
            dist.mkdir()
            # A directory where a file is expected — read_text raises OSError.
            (dist / "VERSION").mkdir()
            self.assertTrue(self._detect(dist, Path(tmp) / "server"))


class ResolvedValueTests(TestCase):
    def test_the_setting_resolves_to_something_usable(self):
        from django.conf import settings

        version = settings.VIGIL_AGENT_VERSION
        self.assertTrue(version)
        self.assertNotIn("$", version, "an unexpanded compose variable")

    def test_it_matches_the_agent_shipped_in_this_repo(self):
        """The whole point: bumping the agent is all a release has to do."""
        from django.conf import settings

        source = (Path(vigil_settings.BASE_DIR).parent / "agent"
                  / "vigil_agent" / "__version__.py")
        if not source.is_file():
            self.skipTest("agent source not present in this build")
        import re

        declared = re.search(r"""__version__\s*=\s*["']([^"']+)["']""",
                             source.read_text()).group(1)
        self.assertEqual(settings.VIGIL_AGENT_VERSION, declared)

    def test_it_is_assigned_from_the_detector_not_a_literal(self):
        """Parsed rather than grepped: the docstring in settings quotes the old
        expression and the versions that drifted, and should keep doing so."""
        self.assertEqual(_agent_version_assignment(), "_detect_agent_version")


class EnvIsInertTests(TestCase):
    """Honouring the variable is what let a stale compose default pin the
    fleet. It must not come back."""

    def test_settings_does_not_read_the_variable_for_its_value(self):
        """The exact shape of the old bug: os.environ.get(..., <literal>)."""
        self.assertNotEqual(_agent_version_assignment(), "os.environ.get")

    def test_a_set_variable_does_not_change_the_detected_version(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "agent_dist"
            dist.mkdir()
            (dist / "VERSION").write_text("2026.6.2\n")
            with patch.dict("os.environ", {"VIGIL_AGENT_VERSION": "2026.3.8"}), \
                 patch.object(vigil_settings, "VIGIL_AGENT_DIST_DIR", dist), \
                 patch.object(vigil_settings, "BASE_DIR", Path(tmp) / "server"):
                self.assertEqual(vigil_settings._detect_agent_version(), "2026.6.2")

    def test_a_set_variable_is_reported_not_swallowed(self):
        from apps.hosts.apps import HostsConfig

        with override_settings(VIGIL_AGENT_VERSION_ENV_IGNORED=True,
                               VIGIL_AGENT_VERSION="2026.6.2"), \
                patch.dict("os.environ", {"VIGIL_AGENT_VERSION": "2026.3.8"}):
            with self.assertLogs("apps.hosts.apps", level="WARNING") as logs:
                HostsConfig._warn_if_agent_version_pinned()
        joined = "\n".join(logs.output)
        self.assertIn("2026.3.8", joined, "say what they set")
        self.assertIn("2026.6.2", joined, "say what is actually being used")
        self.assertIn("ignored", joined.lower())

    def test_nothing_is_logged_when_the_variable_is_unset(self):
        import logging

        from apps.hosts.apps import HostsConfig

        with override_settings(VIGIL_AGENT_VERSION_ENV_IGNORED=False):
            with patch.object(logging.getLogger("apps.hosts.apps"),
                              "warning") as warn:
                HostsConfig._warn_if_agent_version_pinned()
        warn.assert_not_called()


class ComposeTests(TestCase):
    """The shipped compose file passed the variable on every deploy, so its
    default overrode settings everywhere. That is why the drift was invisible."""

    def _compose(self):
        path = Path(vigil_settings.BASE_DIR).parent / "docker-compose.yml"
        if not path.is_file():
            self.skipTest("docker-compose.yml not present in this build")
        return path.read_text()

    def test_compose_no_longer_passes_the_variable(self):
        for line in self._compose().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("VIGIL_AGENT_VERSION:", stripped)

    def test_compose_carries_no_stale_agent_version_default(self):
        self.assertNotIn("2026.3.8", self._compose())
