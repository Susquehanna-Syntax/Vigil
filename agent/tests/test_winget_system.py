r"""winget as LocalSystem: the VCLibs PATH fix, and denials that get reported.

Two defects from the Windows VM (2026-09-29), both under the accounts the agent
ships as:

``%ProgramFiles%\WindowsApps\…\winget.exe`` is a *packaged* binary. Launched
outside its package context — which is what a service does — its loader cannot
find the VCLibs C runtime and it exits -1073741515 (0xC0000135,
STATUS_DLL_NOT_FOUND). Probe as ``nt authority\system`` on the VM: plain exit
-1073741515, and with the VCLibs package directory put first on PATH, exit 0
plus a real ``winget list``. So every Windows agent running as LocalSystem lost
winget inventory and ``install_package`` / ``update_package`` /
``remove_package`` / ``run_package_updates`` outright.

And where the agent is refused — ``WindowsApps`` by ACL under
``NT SERVICE\vigil-agent``, another user's registry hive the same way — it
skipped the source in silence, so the server showed four items and no hint why.
Both denials are now ``errors`` entries the operator can act on.

Nothing here runs winget and nothing here opens a registry; the safety-net guard
is loaded for that reason too. ``tests._winenv`` supplies the fake package tree
and the Windows process facts the branches under test need.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
import tests._safety_net  # noqa: F401  — installed before anything here can spawn
from tests._winenv import (
    APP_INSTALLER,
    OLD_PATH,
    VCLIBS_NEW,
    VCLIBS_OLD,
    VCLIBS_X86,
    FakeProgramFiles,
    windows_machine,
)

# isort: split
from vigil_agent import pkg_manager, software

SID_A = "S-1-5-21-1111111111-2222222222-3333333333-1001"
SID_B = "S-1-5-21-1111111111-2222222222-3333333333-1002"
SID_C = "S-1-5-21-1111111111-2222222222-3333333333-1003"


class WingetEnvTests(unittest.TestCase):
    def test_winget_env_puts_vclibs_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(
                APP_INSTALLER + "\\winget.exe", VCLIBS_OLD, VCLIBS_NEW, VCLIBS_X86)
            with windows_machine(pkg_manager, apps):
                env = pkg_manager.winget_env(str(apps.winget()))
            parts = env["PATH"].split(os.pathsep)

            self.assertEqual(parts[0], apps.package(VCLIBS_NEW),
                             "the newest x64 UWPDesktop runtime goes first; "
                             f"got {parts[0]}")
            self.assertEqual(parts[1], apps.package(APP_INSTALLER),
                             "winget's own directory comes next")
            self.assertEqual(parts[2:], OLD_PATH,
                             "and the account's PATH stays behind them")
            self.assertNotIn(apps.package(VCLIBS_OLD), parts,
                             "the older runtime must not shadow the newer one")
            self.assertNotIn(apps.package(VCLIBS_X86), parts,
                             "an x86 runtime cannot satisfy an x64 winget")

    def test_winget_env_plain_when_not_windowsapps(self):
        r"""A per-user App Execution Alias is not the machine-wide package.

        Its path ends in ``WindowsApps\winget.exe``, so a substring test would
        take it for the packaged binary; only the machine-wide directory counts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(
                APP_INSTALLER + "\\winget.exe", VCLIBS_NEW)
            with windows_machine(pkg_manager, apps):
                env = pkg_manager.winget_env(apps.per_user_winget())
                plain = pkg_manager.clean_env()

            self.assertEqual(env, plain)

    def test_winget_env_plain_when_no_vclibs_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(APP_INSTALLER + "\\winget.exe")
            with windows_machine(pkg_manager, apps):
                env = pkg_manager.winget_env(str(apps.winget()))
                plain = pkg_manager.clean_env()

            self.assertEqual(env, plain)

    def test_winget_env_never_raises_when_listing_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(
                APP_INSTALLER + "\\winget.exe", VCLIBS_NEW)
            with windows_machine(pkg_manager, apps), \
                    patch.object(pkg_manager.Path, "glob",
                                 side_effect=PermissionError(13, "denied")):
                env = pkg_manager.winget_env(str(apps.winget()))
                plain = pkg_manager.clean_env()

            self.assertEqual(env, plain)


class ResolveWingetTests(unittest.TestCase):
    def test_resolve_winget_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(APP_INSTALLER + "\\winget.exe")
            with windows_machine(pkg_manager, apps), \
                    patch.object(pkg_manager.Path, "glob",
                                 side_effect=PermissionError(13, "denied")):
                self.assertEqual(pkg_manager.resolve_winget(), ("", "denied"))
                self.assertEqual(pkg_manager._resolve_winget(), "")

    def test_resolve_winget_absent_when_the_package_is_not_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(VCLIBS_NEW)
            with windows_machine(pkg_manager, apps):
                self.assertEqual(pkg_manager.resolve_winget(), ("", "absent"))

    def test_resolve_winget_finds_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(APP_INSTALLER + "\\winget.exe")
            with windows_machine(pkg_manager, apps):
                path, reason = pkg_manager.resolve_winget()

            self.assertEqual(path, str(apps.winget()))
            self.assertEqual(reason, "")


class PackageManagerEnvTests(unittest.TestCase):
    def test_package_manager_runs_winget_with_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = FakeProgramFiles(tmp).install(
                APP_INSTALLER + "\\winget.exe", VCLIBS_NEW)
            with windows_machine(pkg_manager, apps):
                pm = pkg_manager.PackageManager(name="winget", path=str(apps.winget()))
                with patch.object(pkg_manager.subprocess, "run") as run:
                    run.return_value.returncode = 0
                    run.return_value.stdout = "ok"
                    run.return_value.stderr = ""
                    pm.install("Foo.Bar")
                argv = run.call_args.args[0]
                env = run.call_args.kwargs["env"]

            self.assertEqual([argv[0], argv[1]], [str(apps.winget()), "install"])
            self.assertEqual(env["PATH"].split(os.pathsep)[0],
                             apps.package(VCLIBS_NEW))

    def test_other_managers_get_no_env_of_their_own(self):
        self.assertIsNone(pkg_manager.PackageManager(name="apt-get")._env())


class _Handle:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """The slice of ``winreg`` the user-hive loop uses.

    ``denied_sids`` refuse the open; ``missing_sids`` have no per-user
    ``Uninstall`` key at all, which is not a denial.
    """

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_USERS = "HKU"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100

    def __init__(self, sids=(), denied_sids=(), missing_sids=()):
        self.sids = list(sids)
        self.denied = set(denied_sids)
        self.missing = set(missing_sids)

    def OpenKey(self, hkey, path, reserved=0, access=0):
        if hkey is self.HKEY_USERS:
            if path == "":
                return _Handle("users")
            sid = path.split("\\")[0]
            if sid in self.denied:
                raise PermissionError(13, "denied", path)
            if sid in self.missing:
                raise FileNotFoundError(2, "no such key", path)
            raise AssertionError(f"unexpected hive open: {path}")
        if path.endswith("Wow6432Node"):
            raise FileNotFoundError(2, "no 32-bit view", path)
        return _Handle(path)

    def EnumKey(self, handle, index):
        if handle.name == "users" and index < len(self.sids):
            return self.sids[index]
        raise OSError(2, "no more data")


class ReadUninstallKeysDeniedTests(unittest.TestCase):
    def _run(self, winreg):
        denied: list[str] = []
        with patch.object(software, "_winreg", return_value=winreg):
            rows = software._read_uninstall_keys(denied)
        return rows, denied

    def test_denied_user_hives_are_collected(self):
        rows, denied = self._run(_FakeWinreg(
            sids=[SID_A, SID_B], denied_sids={SID_A, SID_B}))
        self.assertEqual(rows, [])
        self.assertEqual(denied, [SID_A, SID_B])

    def test_missing_user_uninstall_key_is_silent(self):
        rows, denied = self._run(_FakeWinreg(
            sids=[SID_C], missing_sids={SID_C}))
        self.assertEqual(rows, [])
        self.assertEqual(denied, [],
                         "a user with no per-user installs is not a denial")

    def test_without_a_list_a_denial_still_skips_the_hive(self):
        with patch.object(software, "_winreg", return_value=_FakeWinreg(
                sids=[SID_A], denied_sids={SID_A})):
            self.assertEqual(software._read_uninstall_keys(), [])


class CollectWindowsDenialsTests(unittest.TestCase):
    """A denial reaches the payload's ``errors``; a plain skip does not."""

    def _collect(self, resolve_reason, denied_sids):
        def read_keys(denied=None):
            if denied is not None:
                denied.extend(denied_sids)
            return []

        with patch.object(software, "_winget_binary", return_value=None), \
                patch.object(software.shutil, "which", return_value=None), \
                patch.object(software, "_read_uninstall_keys",
                             side_effect=read_keys), \
                patch.object(pkg_manager, "resolve_winget",
                             return_value=("", resolve_reason)):
            return software.collect_windows()

    def test_collect_windows_reports_denials(self):
        payload = self._collect("denied", [SID_A, SID_B])
        self.assertIn("WindowsApps access denied", payload["errors"]["winget"])
        self.assertIn("run the agent as LocalSystem",
                      payload["errors"]["winget"])
        self.assertEqual(
            payload["errors"]["registry-users"],
            "2 user hive(s) unreadable from this account (access denied) — "
            "run the agent as LocalSystem")

    def test_absent_winget_and_readable_hives_stay_silent(self):
        payload = self._collect("absent", [])
        self.assertEqual(payload["errors"], {})


if __name__ == "__main__":
    unittest.main()
