"""The Linux side of the app actions — argv, source rules, outputs, re-collection.

Nothing here may run a real command: ``pkg_manager.detect``, ``pkg_manager._run``
and ``software.collect_now`` are patched for every test and the fakes record
what they were asked to do. The primary-manager path goes through
``PackageManager.install``/``remove``, which re-validates the name before the
fake sees it, so a name the agent ought to refuse fails here instead of quietly
becoming a command line.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import executor, pkg_manager, software
from vigil_agent.actions import apps
from vigil_agent.config import _ALL_ACTIONS, AgentConfig

_CONFIG = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40)

_STORE_ARGV = {
    "snap": {
        "install": ["snap", "install", "firefox"],
        "upgrade": ["snap", "refresh", "firefox"],
        "uninstall": ["snap", "remove", "firefox"],
    },
    "flatpak": {
        "install": ["flatpak", "install", "-y", "--noninteractive", "flathub",
                    "firefox"],
        "upgrade": ["flatpak", "update", "-y", "--noninteractive", "firefox"],
        "uninstall": ["flatpak", "uninstall", "-y", "--noninteractive",
                      "firefox"],
    },
}


def _payload(*items):
    return {"items": [dict(item) for item in items]}


class _FakePM:
    """Records what the handler asked the host's package manager to do."""

    def __init__(self, name="apt"):
        self.name = name
        self.calls = []
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1
        return ""

    def upgrade_all(self):
        self.calls.append(("upgrade_all",))
        return ""

    def install(self, package_name):
        pkg_manager._validate_package_name(package_name)
        self.calls.append(("install", package_name))
        return ""

    def remove(self, package_name):
        pkg_manager._validate_package_name(package_name)
        self.calls.append(("remove", package_name))
        return ""


class _RecordingRun:
    """The substituted ``_run``: records argv, fails on a matching token."""

    def __init__(self, fail_for=()):
        self.argvs = []
        self.fail_for = tuple(fail_for)

    def __call__(self, argv, **_kwargs):
        if any(token in argv for token in self.fail_for):
            raise RuntimeError(f"Command exited 1: {' '.join(argv)}")
        self.argvs.append(list(argv))
        return ""


class AppActionsLinuxTests(unittest.TestCase):
    def setUp(self):
        self.pm = _FakePM("apt")
        self.run = _RecordingRun()
        self.payloads = []
        # The handlers call pkg_manager.detect() and pkg_manager._run(...), so
        # those are the names patched; keep the started mocks, not the patchers.
        patchers = (mock.patch.object(pkg_manager, "detect", return_value=self.pm),
                    mock.patch.object(pkg_manager, "_run", side_effect=self.run),
                    mock.patch.object(software, "collect_now",
                                      side_effect=self._next_payload))
        self.detect, self.run_patch, self.collect = (p.start() for p in patchers)
        for patcher in patchers:
            self.addCleanup(patcher.stop)

    def fail_run_on(self, token):
        """Make the substituted _run fail for any argv containing *token*."""
        self.run.fail_for = (token,)

    def _next_payload(self):
        return self.payloads.pop(0) if self.payloads else _payload()

    def use_pm(self, pm):
        """Swap the detected manager *before* the handler runs."""
        self.pm = pm
        self.detect.return_value = pm
        self.run_patch.side_effect = self.run

    # ── primary manager ───────────────────────────────────────────────────

    def test_install_primary_and_at_version(self):
        out = apps._app_install({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.calls, [("install", "openssl")])
        self.assertEqual(self.pm.refreshes, 1, "install refreshes first")
        self.assertEqual(out.data["source"], "dpkg")

        apps._app_install({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(self.pm.calls[-1], ("install", "openssl=3.0.13-1"))

        dnf = _FakePM("dnf")
        self.use_pm(dnf)
        apps._app_install({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(dnf.calls, [("install", "openssl-3.0.13-1")])

    def test_pacman_refuses_a_version_pin(self):
        pacman = _FakePM("pacman")
        self.use_pm(pacman)
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_install({"app": "openssl", "version": "3.0.13-1"},
                              _CONFIG)
        self.assertIn("version pinning is not supported for pacman",
                      str(ctx.exception))
        self.assertEqual(pacman.calls, [], "the refusal installed nothing")

    def test_upgrade_one_app_uses_install(self):
        out = apps._app_upgrade({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.calls, [("install", "openssl")])
        self.assertEqual(out.data, {"upgraded": 1, "failed": 0})

    def test_upgrade_all_counts_what_stopped_being_outdated(self):
        self.payloads = [
            _payload({"source": "dpkg", "id": "openssl", "version": "3.0.13",
                      "latest": "3.0.13-1"},
                     {"source": "dpkg", "id": "curl", "version": "8.5",
                      "latest": "8.7"},
                     {"source": "dpkg", "id": "stable", "version": "1.0"}),
            _payload({"source": "dpkg", "id": "openssl", "version": "3.0.13-1"},
                     {"source": "dpkg", "id": "curl", "version": "8.5",
                      "latest": "8.7"},
                     {"source": "dpkg", "id": "stable", "version": "1.0"}),
        ]
        out = apps._app_upgrade({}, _CONFIG)
        self.assertEqual(self.pm.calls, [("upgrade_all",)])
        self.assertEqual(out.data, {"upgraded": 1, "failed": 0})

    # ── snap / flatpak ────────────────────────────────────────────────────

    def test_snap_and_flatpak_commands(self):
        handlers = {"install": apps._app_install,
                    "upgrade": apps._app_upgrade,
                    "uninstall": apps._app_uninstall}
        for action, handler in handlers.items():
            for store in ("snap", "flatpak"):
                with self.subTest(store=store, action=action):
                    self.run.argvs = []
                    handler({"app": "firefox", "source": store}, _CONFIG)
                    self.assertEqual(self.run.argvs,
                                     [_STORE_ARGV[store][action]])

    def test_snap_upgrade_without_a_name_refreshes_everything(self):
        # Before: firefox outdated; after: current — so one app moved.
        self.payloads = [
            _payload({"source": "snap", "id": "firefox", "latest": "156.0.1-1"}),
            _payload({"source": "snap", "id": "firefox", "latest": ""}),
        ]
        out = apps._app_upgrade({"source": "snap"}, _CONFIG)
        self.assertEqual(self.run.argvs, [["snap", "refresh"]])
        self.assertEqual(out.data, {"upgraded": 1, "failed": 0})

    def test_flatpak_upgrade_without_a_name_updates_everything(self):
        out = apps._app_upgrade({"source": "flatpak"}, _CONFIG)
        self.assertEqual(self.run.argvs,
                         [["flatpak", "update", "-y", "--noninteractive"]])
        self.assertEqual(out.data, {"upgraded": 0, "failed": 0})

    def test_stores_never_reach_the_package_manager(self):
        apps._app_install({"app": "firefox", "source": "snap"}, _CONFIG)
        self.assertEqual(self.pm.calls, [])
        self.assertEqual(self.pm.refreshes, 0)

    def test_version_pin_refused_for_snap_and_flatpak(self):
        for store in ("snap", "flatpak"):
            with self.subTest(store=store):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_install({"app": "firefox", "source": store,
                                       "version": "125"}, _CONFIG)
                self.assertIn("version pinning is not supported",
                              str(ctx.exception))
        self.assertEqual(self.run.argvs, [])

    # ── source rules ──────────────────────────────────────────────────────

    def test_source_mismatch_and_windows_sources_refused(self):
        self.use_pm(_FakePM("dnf"))
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_install({"app": "openssl", "source": "dpkg"}, _CONFIG)
        self.assertEqual(
            "source dpkg is not this host's package manager (dnf, source rpm)",
            str(ctx.exception))

        for source in ("winget", "chocolatey", "scoop", "registry"):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_install({"app": "Firefox", "source": source},
                                      _CONFIG)
                self.assertEqual(f"source {source} is Windows-only",
                                 str(ctx.exception))
        self.assertEqual(self.run.argvs, [])
        self.assertEqual(self.pm.calls, [])

    def test_a_primary_manager_name_is_not_a_source(self):
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "openssl", "source": "apt"}, _CONFIG)
        self.assertIn("unknown source 'apt'", str(ctx.exception))

    def test_inventory_source_names_work_on_an_apt_host(self):
        # Found on the Ubuntu VM: the Apps page says "dpkg", the agent compared it
        # with the manager's name "apt-get" and refused.
        self.payloads = [_payload({"source": "dpkg", "id": "cowsay", "version": "3.03"})]
        out = apps._app_install({"app": "cowsay", "source": "dpkg"}, _CONFIG)
        self.assertEqual(out.data, {"installed_version": "3.03", "source": "dpkg"})
        self.payloads = [_payload({"source": "dpkg", "id": "cowsay", "version": "3.03"})]
        out = apps._app_uninstall({"app": "cowsay", "source": "dpkg"}, _CONFIG)
        self.assertEqual(out.data, {"removed": False}, "still listed under dpkg")

    def test_no_source_uses_the_primary_manager(self):
        apps._app_install({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.calls, [("install", "openssl")])
        apps._app_uninstall({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.calls[-1], ("remove", "openssl"))

    def test_uninstall_does_not_refresh(self):
        apps._app_uninstall({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.refreshes, 0)

    # ── outputs and re-collection ─────────────────────────────────────────

    def test_install_reports_installed_version_from_the_collection(self):
        self.payloads = [_payload({"source": "dpkg", "id": "openssl",
                                   "version": "3.0.13-1"})]
        out = apps._app_install({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data, {"installed_version": "3.0.13-1",
                                      "source": "dpkg"})

    def test_snap_install_version_comes_from_the_collection(self):
        self.payloads = [_payload({"source": "snap", "id": "firefox",
                                   "version": "126.0.1"})]
        out = apps._app_install({"app": "firefox", "source": "snap"}, _CONFIG)
        self.assertEqual(out.data, {"installed_version": "126.0.1",
                                      "source": "snap"})

    def test_uninstall_removed_true_when_the_id_is_gone(self):
        self.payloads = [_payload({"source": "dpkg", "id": "curl",
                                   "version": "8.5"})]
        out = apps._app_uninstall({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data, {"removed": True})

    def test_uninstall_removed_false_when_still_listed(self):
        self.payloads = [_payload({"source": "dpkg", "id": "openssl",
                                   "version": "3.0.13-1"})]
        out = apps._app_uninstall({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data, {"removed": False})

    def test_recollect_after_success_not_after_failure(self):
        apps._app_install({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.collect.call_count, 1)

        self.fail_run_on("install")
        with self.assertRaises(RuntimeError):
            apps._app_install({"app": "firefox", "source": "snap"}, _CONFIG)
        self.assertEqual(self.collect.call_count, 1,
                         "a failed command must not re-collect")

    def test_raising_recollect_does_not_fail_a_successful_action(self):
        self.collect.side_effect = RuntimeError("collection failed")
        out = apps._app_install({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data["source"], "dpkg")
        self.assertEqual(out.data["installed_version"], "")
        out = apps._app_uninstall({"app": "openssl"}, _CONFIG)
        self.assertIs(out.data["removed"], True)

    # ── the agent re-validates what the wire gave it ──────────────────────

    def test_agent_revalidates_app(self):
        for bad in ("foo;rm -rf /", "-oProxy=x", "-oProxy", "a b", "openssl|ls", ""):
            with self.subTest(app=bad):
                with self.assertRaises(RuntimeError):
                    apps._app_install({"app": bad}, _CONFIG)
                with self.assertRaises(RuntimeError):
                    apps._app_uninstall({"app": bad}, _CONFIG)
                # snap/flatpak commands reach _run directly: the app check is
                # their only guard (no _validate_package_name behind it).
                for store in ("snap", "flatpak"):
                    with self.assertRaises(RuntimeError):
                        apps._app_install({"app": bad, "source": store}, _CONFIG)
        self.assertEqual(self.run.argvs, [])
        self.assertEqual(self.pm.calls, [], "a refused app ran no command")
        self.assertEqual(self.collect.call_count, 0,
                         "a refused app did not re-collect")

    def test_agent_revalidates_version(self):
        with self.assertRaises(RuntimeError):
            apps._app_install({"app": "openssl", "version": "3.0 13"}, _CONFIG)
        self.assertEqual(self.pm.calls, [])

    def test_inventory_ids_with_hyphens_and_colons_are_accepted(self):
        apps._app_install({"app": "libc6:amd64"}, _CONFIG)
        apps._app_uninstall({"app": "python3-pip"}, _CONFIG)
        self.assertEqual(self.pm.calls, [("install", "libc6:amd64"),
                                         ("remove", "python3-pip")])

    def test_handlers_are_wired_and_allowlistable(self):
        for name, handler in (("app_install", apps._app_install),
                              ("app_upgrade", apps._app_upgrade),
                              ("app_uninstall", apps._app_uninstall)):
            self.assertIs(executor._HANDLERS[name], handler)
            self.assertIn(name, _ALL_ACTIONS)


if __name__ == "__main__":
    unittest.main()
