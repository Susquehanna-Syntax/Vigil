"""FreeBSD ``pkg`` support: the package manager's argv and the software inventory.

Nothing here runs a real command. ``platform.system``, ``pkg_manager._which``,
``pkg_manager._run``, ``software._run`` and ``software._which`` are patched, and
the fakes record what they were asked. The pkg output formats are assumed from
the pkg(8) manual (``pkg query``, ``pkg version -vRL=``), not recorded on a
FreeBSD host.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import pkg_manager, software

_QUERY_TEXT = (
    "curl\t8.10.1\tftp/curl\tgabor@FreeBSD.org\n"
    "py311-requests\t2.32.3\tdevel/py311-requests\tpython@FreeBSD.org\n"
    "openssl\t3.0.15\tsecurity/openssl\tsecurity@FreeBSD.org\n"
)

_UPDATES_TEXT = (
    "curl-8.10.1                        <   needs updating (remote has 8.11.0)\n"
    "py311-requests-2.32.3              =   up-to-date with remote\n"
    "openssl-3.0.15                     <   needs updating (remote has 3.0.16)\n"
)


class PkgDetectTests(unittest.TestCase):
    def test_detect_returns_pkg_on_freebsd(self):
        with mock.patch.object(pkg_manager.platform, "system", return_value="FreeBSD"), \
             mock.patch.object(pkg_manager, "_which", side_effect=lambda n: n == "pkg"):
            pm = pkg_manager.detect()
        self.assertIsNotNone(pm)
        self.assertEqual(pm.name, "pkg")

    def test_detect_freebsd_ignores_linux_managers(self):
        probed = []

        def which(name):
            probed.append(name)
            return name == "apt-get"

        with mock.patch.object(pkg_manager.platform, "system", return_value="FreeBSD"), \
             mock.patch.object(pkg_manager, "_which", side_effect=which):
            pm = pkg_manager.detect()
        self.assertIsNone(pm)
        self.assertEqual(probed, ["pkg"])


class PkgArgvTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.run = mock.patch.object(pkg_manager, "_run", side_effect=self._record).start()
        self.addCleanup(mock.patch.stopall)
        self.pm = pkg_manager.PackageManager(name="pkg")

    def _record(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return ""

    def test_refresh_argv(self):
        self.pm.refresh()
        self.assertEqual(self.calls[-1][0], ["pkg", "update", "-q"])

    def test_upgrade_argv(self):
        self.pm.upgrade_all()
        self.assertEqual(self.calls[-1][0], ["pkg", "upgrade", "-y"])

    def test_install_argv(self):
        self.pm.install("py311-requests")
        self.assertEqual(self.calls[-1][0], ["pkg", "install", "-y", "py311-requests"])

    def test_remove_argv(self):
        self.pm.remove("curl")
        self.assertEqual(self.calls[-1][0], ["pkg", "delete", "-y", "curl"])

    def test_list_upgradable_argv_and_short_timeout(self):
        self.pm.list_upgradable()
        argv, kwargs = self.calls[-1]
        self.assertEqual(argv, ["pkg", "version", "-vRL="])
        self.assertEqual(kwargs.get("timeout"), pkg_manager._EXEC_TIMEOUT_SHORT)

    def test_installed_version_argv(self):
        self.run.side_effect = self._record_version
        self.assertEqual(self.pm.installed_version("curl"), "8.10.1")
        self.assertEqual(self.calls[-1][0], ["pkg", "query", "%v", "curl"])

    def _record_version(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return "8.10.1\n"

    def test_install_refuses_bad_name_before_running(self):
        with self.assertRaises(ValueError):
            self.pm.install("curl; rm -rf /")
        self.assertEqual(self.calls, [])


class PkgRunExitCodeTests(unittest.TestCase):
    def test_exit_zero_returns_output(self):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        with mock.patch.object(pkg_manager.subprocess, "run", return_value=proc):
            self.assertEqual(pkg_manager._run(["pkg", "update", "-q"]), "ok")

    def test_nonzero_other_than_100_raises(self):
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(pkg_manager.subprocess, "run", return_value=proc):
            with self.assertRaises(RuntimeError):
                pkg_manager._run(["pkg", "install", "-y", "nope"])


class PkgParseTests(unittest.TestCase):
    def test_parse_pkg_list(self):
        items = software._parse_pkg_list(_QUERY_TEXT)
        self.assertEqual(len(items), 3)
        curl = items[0]
        self.assertEqual(curl["source"], "pkg")
        self.assertEqual(curl["id"], "curl")
        self.assertEqual(curl["name"], "curl")
        self.assertEqual(curl["version"], "8.10.1")
        self.assertEqual(curl["publisher"], "gabor@FreeBSD.org")
        self.assertEqual(curl["latest"], "")
        self.assertEqual(set(curl), set(software.ITEM_FIELDS))

    def test_parse_pkg_list_skips_blank_and_malformed_rows(self):
        self.assertEqual(software._parse_pkg_list("\n\nlonely\n"), [])

    def test_parse_pkg_updates(self):
        updates = software._parse_pkg_updates(_UPDATES_TEXT)
        self.assertEqual(updates, {"curl": "8.11.0", "openssl": "3.0.16"})

    def test_parse_pkg_updates_hyphenated_name_splits_on_last_hyphen(self):
        text = "py311-requests-2.32.3    <   needs updating (remote has 2.32.4)\n"
        self.assertEqual(software._parse_pkg_updates(text), {"py311-requests": "2.32.4"})

    def test_parse_pkg_updates_ignores_up_to_date(self):
        text = "py311-requests-2.32.3              =   up-to-date with remote\n"
        self.assertEqual(software._parse_pkg_updates(text), {})


class PkgCollectTests(unittest.TestCase):
    def _fake_run(self, query=_QUERY_TEXT, updates=_UPDATES_TEXT, fail_updates=False):
        argvs = []

        def run(argv, timeout=60, env=None):
            argvs.append(list(argv))
            if argv == software._PKG_ARGV:
                return 0, query, ""
            if argv == software._PKG_UPDATES_ARGV:
                if fail_updates:
                    return 1, "", "pkg: no remote"
                return 0, updates, ""
            raise AssertionError(f"unexpected argv {argv}")

        return run, argvs

    def test_collect_pkg_merges_latest_into_matching_items(self):
        run, _ = self._fake_run()
        errors = {}
        with mock.patch.object(software, "_run", side_effect=run):
            items = software._collect_pkg(errors)
        by_name = {item["name"]: item for item in items}
        self.assertEqual(by_name["curl"]["latest"], "8.11.0")
        self.assertEqual(by_name["openssl"]["latest"], "3.0.16")
        self.assertEqual(by_name["py311-requests"]["latest"], "")
        self.assertEqual(errors, {})

    def test_collect_pkg_update_failure_keeps_items(self):
        run, _ = self._fake_run(fail_updates=True)
        errors = {}
        with mock.patch.object(software, "_run", side_effect=run):
            items = software._collect_pkg(errors)
        self.assertEqual(len(items), 3)
        self.assertIn("pkg-updates", errors)
        self.assertNotIn("pkg", errors)

    def test_collect_linux_uses_pkg_on_freebsd(self):
        run, argvs = self._fake_run()
        with mock.patch.object(software.sys, "platform", "freebsd14"), \
             mock.patch.object(software, "_which", side_effect=lambda b: b == "pkg"), \
             mock.patch.object(software, "_run", side_effect=run):
            payload = software.collect_linux()
        self.assertIn(software._PKG_ARGV, argvs)
        self.assertEqual({item["source"] for item in payload["items"]}, {"pkg"})
        self.assertEqual(len(payload["items"]), 3)

    def test_collect_linux_never_uses_pkg_off_freebsd(self):
        run, argvs = self._fake_run()
        with mock.patch.object(software.sys, "platform", "linux"), \
             mock.patch.object(software, "_which", side_effect=lambda b: b == "pkg"), \
             mock.patch.object(software, "_run", side_effect=run):
            payload = software.collect_linux()
        self.assertEqual(argvs, [])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["errors"], {})


if __name__ == "__main__":
    unittest.main()


class PkgPinTests(unittest.TestCase):
    """app_pin on FreeBSD uses pkg's own lock, never apt-mark."""

    def test_pin_and_unpin_use_pkg_lock(self):
        from vigil_agent import executor  # noqa: F401 — importing actions.apps first is circular
        from vigil_agent.actions import apps
        self.assertEqual(apps._pin_commands("pkg", "", unpin=False), [["hold"]])
        self.assertEqual(apps._pin_argv(["hold"], "pkg", "curl", "", "pkg"),
                         ["pkg", "lock", "-y", "curl"])
        self.assertEqual(apps._pin_argv(["unhold"], "pkg", "curl", "", "pkg"),
                         ["pkg", "unlock", "-y", "curl"])

    def test_a_chosen_version_is_refused_before_any_command(self):
        from vigil_agent import executor  # noqa: F401 — importing actions.apps first is circular
        from vigil_agent.actions import apps
        with self.assertRaises(RuntimeError) as caught:
            apps._pin_commands("pkg", "8.10.1", unpin=False)
        self.assertIn("pkg cannot pin a chosen version", str(caught.exception))
