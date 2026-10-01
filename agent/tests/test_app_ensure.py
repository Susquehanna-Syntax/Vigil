"""``app_ensure`` — the policy decision table, run on the host's own inventory.

The four handlers it delegates to and ``_recollect`` are patched: each test
says what the inventory holds before and after, and checks which handler ran
with which params — and that nothing ran when nothing needed to.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import executor  # noqa: F401 — import before actions.apps (circular)
from vigil_agent.actions import apps
from vigil_agent.config import _ALL_ACTIONS, AgentConfig

_CONFIG = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40)


def _item(version, latest="", source="dpkg", ident="curl"):
    return {"source": source, "id": ident, "version": version, "latest": latest}


def _payload(*items):
    return {"items": list(items)}


class AppEnsureTests(unittest.TestCase):
    def setUp(self):
        self.handlers = {}
        for name in ("_app_install", "_app_upgrade", "_app_uninstall", "_app_pin"):
            p = mock.patch.object(apps, name)
            self.handlers[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(apps, "_recollect")
        self.recollect = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(apps.sys, "platform", "linux")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, before, after=None, **params):
        self.recollect.side_effect = [before, after if after is not None else before]
        return apps._app_ensure(params, _CONFIG)

    def _ran(self):
        return {name: m.call_args.args[0] for name, m in self.handlers.items() if m.called}

    def test_decision_table(self):
        current = _payload(_item("8.5", "8.5"))
        outdated = _payload(_item("8.4", "8.5"))
        empty = _payload()
        cases = [
            ("present", "", empty, "install"),
            ("present", "", current, "none"),
            ("latest", "", empty, "install"),
            ("latest", "", outdated, "upgrade"),
            ("latest", "", current, "none"),
            ("pinned", "8.0", empty, "pin"),
            ("pinned", "8.0", current, "pin"),
            ("pinned", "8.5", current, "none"),
            ("absent", "", empty, "none"),
            ("absent", "", current, "uninstall"),
        ]
        handler = {"install": "_app_install", "upgrade": "_app_upgrade",
                   "uninstall": "_app_uninstall", "pin": "_app_pin"}
        for state, version, before, expected in cases:
            with self.subTest(state=state, before=before, expected=expected):
                for m in self.handlers.values():
                    m.reset_mock()
                self.recollect.reset_mock()
                params = {"app": "curl", "state": state}
                if version:
                    params["version"] = version
                out = self._run(before, **params)
                self.assertEqual(out.data["action"], expected)
                self.assertEqual(out.data["changed"], expected != "none")
                ran = self._ran()
                if expected == "none":
                    self.assertEqual(ran, {})
                    self.assertEqual(self.recollect.call_count, 1,
                                     "nothing ran, so nothing to re-collect")
                else:
                    self.assertEqual(list(ran), [handler[expected]])

    def test_versions_before_and_after(self):
        out = self._run(_payload(_item("8.4", "8.5")), _payload(_item("8.5", "8.5")),
                        app="curl", state="latest")
        self.assertEqual((out.data["version_before"], out.data["version_after"]),
                         ("8.4", "8.5"))
        self.assertEqual(self._ran()["_app_upgrade"], {"app": "curl", "source": "dpkg"})

    def test_second_run_changes_nothing(self):
        first = self._run(_payload(), _payload(_item("8.5")), app="curl", state="present")
        self.assertTrue(first.data["changed"])
        for m in self.handlers.values():
            m.reset_mock()
        second = self._run(_payload(_item("8.5")), app="curl", state="present")
        self.assertFalse(second.data["changed"])
        self.assertEqual(self._ran(), {})

    def test_pin_passes_the_version(self):
        self._run(_payload(_item("8.5")), app="curl", state="pinned", version="8.0")
        self.assertEqual(self._ran()["_app_pin"],
                         {"app": "curl", "source": "dpkg", "version": "8.0"})

    def test_the_rows_own_source_is_passed_through(self):
        self._run(_payload(_item("2.0", source="registry")), app="curl", state="absent")
        self.assertEqual(self._ran()["_app_uninstall"], {"app": "curl", "source": "registry"})

    def test_a_source_on_the_rule_restricts_the_match(self):
        out = self._run(_payload(_item("8.5", source="snap")), app="curl",
                        state="absent", source="dpkg")
        self.assertEqual(out.data["action"], "none")
        install = self._run(_payload(_item("8.5", source="snap")), app="curl",
                            state="present", source="dpkg")
        self.assertEqual(install.data["action"], "install")
        self.assertEqual(self._ran()["_app_install"], {"app": "curl", "source": "dpkg"})

    def test_windows_pin_installs_the_version_first(self):
        with mock.patch.object(apps.sys, "platform", "win32"):
            self._run(_payload(_item("1.0", source="winget", ident="Git.Git")),
                      app="Git.Git", state="pinned", version="2.40.0")
        want = {"app": "Git.Git", "source": "winget", "version": "2.40.0"}
        self.assertEqual(self._ran()["_app_install"], want)
        self.assertEqual(self._ran()["_app_pin"], want)

    def test_bad_params_are_refused_before_collecting(self):
        bad = [
            {"app": "curl", "state": "sideways"},
            {"app": "curl"},
            {"app": "-oProxy=x", "state": "present"},
            {"state": "present"},
            {"app": "curl", "state": "pinned"},
            {"app": "curl", "state": "latest", "version": "1.0"},
            {"app": "curl", "state": "present", "source": "brew2"},
            {"app": "curl", "state": "pinned", "version": "1 0"},
        ]
        for params in bad:
            with self.subTest(params=params):
                with self.assertRaises(RuntimeError):
                    apps._app_ensure(params, _CONFIG)
        self.recollect.assert_not_called()
        self.assertEqual(self._ran(), {})

    def test_registered_with_the_executor_and_allowlist(self):
        self.assertIn("app_ensure", _ALL_ACTIONS)
        self.assertIs(executor._HANDLERS["app_ensure"], apps._app_ensure)


if __name__ == "__main__":
    unittest.main()
