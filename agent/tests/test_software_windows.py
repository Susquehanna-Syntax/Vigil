"""Windows software collection: winget/choco/scoop parsers, the registry, isolation.

Nothing here touches ``winreg`` or a real command. The three seams are ``_run``,
the binary-presence check (``shutil.which`` / ``software._winget_binary``) and
``_read_uninstall_keys``.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run on its own
import subprocess
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import software

# Captured on the Windows 11 VM 2026-09-29. The spacing is the parser's input:
# column boundaries are the header words' offsets, so re-flow this text and the
# test stops testing the real thing.
WINGET_LIST = """The `msstore` source requires that you view the following agreements before using.
Terms of Transaction: https://aka.ms/microsoft-store-terms-of-transaction
The source requires the current machine's 2-letter geographic region to be sent to the backend service to function properly (ex. "US").

Name                                                     Id                                                                                Version              Available Source
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
App Installer                                            Microsoft.AppInstaller                                                            1.29.379.0           1.29.380  winget
AV1 Video Extension                                      MSIX\\Microsoft.AV1VideoExtension_2.0.35.0_x64__8wekyb3d8bbwe                      2.0.35.0
Copilot                                                  MSIX\\Microsoft.Copilot_1.25121.84.0_x64__8wekyb3d8bbwe                            1.25121.84.0
"""

WINGET_UPGRADE = """Name                  Id                              Version    Available Source
---------------------------------------------------------------------------------
App Installer         Microsoft.AppInstaller          1.29.379.0 1.29.380  winget
Python Launcher       Python.Launcher                 3.12.10    3.14.7    winget
WindowsAppRuntime.1.5 Microsoft.WindowsAppRuntime.1.5 1.5.8      1.5.9     winget
3 upgrades available.
"""

WINGET_LIST_NO_AVAILABLE = """Name                                                     Id                                                                                Version    Source
------------------------------------------------------------------------------------------------------------------------------------------------------------
App Installer                                            Microsoft.AppInstaller                                                            1.29.379.0
VLC                                                      VideoLAN.VLC                                                                      3.0.21     winget
"""

CHOCO_LIST = "git|2.46.0\n7zip|23.1.0\nduct-tape|\n"
CHOCO_OUTDATED = "git|2.46.0|2.47.1|false\n7zip|23.1.0|23.1.0|true\n"

SCOOP_EXPORT = (
    '{"version":"0.8.20260121","apps":['
    '{"Name":"git","Version":"2.47.1","Source":"main","Updated":"2026-02-01 09:11:34"},'
    '{"Name":"ripgrep","Version":"14.1.1","Source":"main","Updated":"2026-02-01 09:12:02"}]}'
)

# The three real HKLM rows from the VM, as _read_uninstall_keys returns them.
ROW_VIRTUALBOX = {
    "hive": "HKLM", "sid": "", "key": "Oracle VirtualBox Guest Additions",
    "DisplayName": "Oracle VirtualBox Guest Additions 7.2.18",
    "DisplayVersion": "7.2.18.175117",
    "Publisher": "Oracle and/or its affiliates", "SystemComponent": None,
    "WindowsInstaller": None, "QuietUninstallString": None,
    "UninstallString": r"C:\Program Files\Oracle\VirtualBox Guest Additions\uninst.exe",
}
ROW_PYTHON_COMPONENT = {
    "hive": "HKLM", "sid": "", "key": "{0158093D-F809-455B-955B-6D16A4B5D118}",
    "DisplayName": "Python 3.12.10 Executables (64-bit)",
    "DisplayVersion": "3.12.10150.0",
    "Publisher": "Python Software Foundation", "SystemComponent": 1,
    "WindowsInstaller": 1, "QuietUninstallString": None,
    "UninstallString": "MsiExec.exe /I{0158093D-F809-455B-955B-6D16A4B5D118}",
}
ROW_COMPAT_DB = {
    "hive": "HKLM", "sid": "", "key": "{22221111-1111-1111-1111-111111111111}.sdb",
    "DisplayName": "Microsoft Windows Application Compatibility Fix Database",
    "DisplayVersion": None, "Publisher": None, "SystemComponent": None,
    "WindowsInstaller": None, "QuietUninstallString": None,
    "UninstallString": r'C:\WINDOWS\system32\sdbinst.exe -u "C:\WINDOWS\AppPatch\CustomSDB'
                       r'\{22221111-1111-1111-1111-111111111111}.sdb"',
}
ROW_FIREFOX = {
    "hive": "HKLM", "sid": "", "key": "FirefoxMozillaFirefox",
    "DisplayName": "Mozilla Firefox (x64 en-US)", "DisplayVersion": "141.0",
    "Publisher": "Mozilla", "SystemComponent": None, "WindowsInstaller": None,
    "QuietUninstallString": None, "UninstallString": r"uninstall.exe",
}
ROW_ALICE = {
    "hive": "HKU", "sid": "S-1-5-21-1-2-3-1001", "key": "Signal.CE507088F127C42F",
    "DisplayName": "Signal", "DisplayVersion": "1.42.4",
    "Publisher": "Open Whisper Systems", "SystemComponent": None,
    "WindowsInstaller": None, "QuietUninstallString": None,
    "UninstallString": r"C:\Users\alice\AppData\Local\Signal\uninstall.exe",
}

_NO_BINARIES = lambda _name: None  # noqa: E731


class _Runner:
    """Answers _run by matching the argv prefix, in table order."""

    def __init__(self, table):
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        # The collector calls winget by its resolved path (C:/winget.exe):
        # match on the program's base name, not the path.
        program = os.path.splitext(os.path.basename(str(argv[0]).replace("\\", "/")))[0]
        argv = [program, *argv[1:]]
        for match, result in self.table:
            if list(argv[:len(match)]) == list(match):
                if isinstance(result, BaseException):
                    raise result
                return result
        return 0, "", ""


def _collect(table, winget=False, present=(), rows=None, rows_error=None):
    """Run collect_windows with all three seams patched."""
    runner = _Runner(table)
    uninstall = software._read_uninstall_keys
    if rows_error is not None:
        uninstall = lambda: (_ for _ in ()).throw(rows_error)  # noqa: E731
    elif rows is not None:
        uninstall = lambda: list(rows)  # noqa: E731
    with patch.object(software, "_run", side_effect=runner), \
            patch.object(software, "_winget_binary",
                         return_value="C:/winget.exe" if winget else None), \
            patch.object(software.shutil, "which",
                         side_effect=lambda b: "C:/bin/" + b if b in present else None), \
            patch.object(software, "_read_uninstall_keys", side_effect=uninstall):
        payload = software.collect_windows()
    return payload, runner


class WingetTableTests(unittest.TestCase):
    def test_winget_table_real_output(self):
        payload, _ = _collect(
            [(["winget", "list"], (0, WINGET_LIST, "")),
             (["winget", "upgrade"], (0, "", ""))],
            winget=True, rows=[])
        self.assertEqual(payload["errors"], {})
        self.assertEqual([i["id"] for i in payload["items"]],
                         ["Microsoft.AppInstaller"])
        item = payload["items"][0]
        self.assertEqual(item["source"], "winget")
        self.assertEqual(item["name"], "App Installer")
        self.assertEqual(item["version"], "1.29.379.0")
        self.assertEqual(item["latest"], "1.29.380")

    def test_winget_upgrade_sets_latest(self):
        payload, _ = _collect(
            [(["winget", "list"], (0, WINGET_UPGRADE, "")),
             (["winget", "upgrade"], (0, WINGET_UPGRADE, ""))],
            winget=True, rows=[])
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(items["Python.Launcher"]["latest"], "3.14.7")
        self.assertEqual(items["Python.Launcher"]["name"], "Python Launcher")
        self.assertEqual(items["Microsoft.WindowsAppRuntime.1.5"]["latest"], "1.5.9")
        self.assertEqual(items["Microsoft.AppInstaller"]["latest"], "1.29.380")
        # The trailing count line is not a row, and nothing is invented from it.
        self.assertNotIn("3 upgrades available.", {i["name"] for i in items.values()})
        self.assertEqual(len(items), 3)

    def test_winget_table_without_available_column(self):
        payload, _ = _collect(
            [(["winget", "list"], (0, WINGET_LIST_NO_AVAILABLE, "")),
             (["winget", "upgrade"], (0, "", ""))],
            winget=True, rows=[])
        items = {i["id"]: i for i in payload["items"]}
        # No Available column: the row's last field is its version, so the
        # Source column is empty and only an explicitly tagged row is winget's.
        self.assertEqual(list(items), ["VideoLAN.VLC"])
        self.assertEqual(items["VideoLAN.VLC"]["version"], "3.0.21")
        self.assertEqual(items["VideoLAN.VLC"]["latest"], "")

    def test_winget_absent_is_not_an_error(self):
        payload, runner = _collect([], winget=False, rows=[ROW_VIRTUALBOX])
        self.assertEqual(runner.calls, [])
        self.assertEqual(payload["errors"], {})
        self.assertEqual([i["source"] for i in payload["items"]], ["registry"])


class PackageManagerTests(unittest.TestCase):
    def test_choco_and_scoop(self):
        payload, _ = _collect(
            [(["choco", "list"], (0, CHOCO_LIST, "")),
             (["choco", "outdated"], (0, CHOCO_OUTDATED, "")),
             (["cmd", "/c", "scoop", "export"], (0, SCOOP_EXPORT, ""))],
            present=("choco", "scoop"), rows=[])
        items = {(i["source"], i["id"]): i for i in payload["items"]}
        self.assertEqual(payload["errors"], {})
        self.assertEqual(items[("chocolatey", "git")]["source"], "chocolatey")
        self.assertEqual(items[("chocolatey", "git")]["name"], "git")
        self.assertEqual(items[("chocolatey", "git")]["version"], "2.46.0")
        self.assertEqual(items[("chocolatey", "git")]["latest"], "2.47.1")
        # A pinned package offers nothing newer.
        self.assertEqual(items[("chocolatey", "7zip")]["latest"], "")
        # A malformed limit-output line is skipped, not an error.
        self.assertNotIn("duct-tape", {k[1] for k in items})
        self.assertEqual(items[("scoop", "ripgrep")]["source"], "scoop")
        self.assertEqual(items[("scoop", "ripgrep")]["name"], "ripgrep")
        self.assertEqual(items[("scoop", "ripgrep")]["version"], "14.1.1")
        self.assertEqual(items[("scoop", "ripgrep")]["latest"], "")

    def test_scoop_invalid_json_is_an_error(self):
        payload, _ = _collect(
            [(["choco", "list"], (0, CHOCO_LIST, "")),
             (["choco", "outdated"], (0, "", "")),
             (["cmd", "/c", "scoop", "export"], (0, "not json at all", ""))],
            present=("choco", "scoop"), rows=[])
        self.assertIn("scoop", payload["errors"])
        self.assertTrue(payload["errors"]["scoop"].startswith("scoop failed:"))
        self.assertEqual({i["source"] for i in payload["items"]}, {"chocolatey"})


class RegistryTests(unittest.TestCase):
    def test_registry_rows_to_items(self):
        payload, _ = _collect([], rows=[ROW_PYTHON_COMPONENT, ROW_VIRTUALBOX,
                                        ROW_COMPAT_DB])
        self.assertEqual(payload["errors"], {})
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(sorted(items),
                         sorted([ROW_VIRTUALBOX["key"], ROW_COMPAT_DB["key"]]))
        self.assertNotIn(ROW_PYTHON_COMPONENT["key"], items)
        vbox = items[ROW_VIRTUALBOX["key"]]
        self.assertEqual(vbox["source"], "registry")
        self.assertEqual(vbox["name"], "Oracle VirtualBox Guest Additions 7.2.18")
        self.assertEqual(vbox["version"], "7.2.18.175117")
        self.assertEqual(vbox["publisher"], "Oracle and/or its affiliates")
        self.assertEqual(vbox["scope"], "machine")
        self.assertEqual(vbox["user"], "")
        self.assertIs(vbox["managed"], False)
        self.assertEqual(items[ROW_COMPAT_DB["key"]]["version"], "")

    def test_registry_claimed_by_winget_is_not_emitted(self):
        payload, _ = _collect(
            [(["winget", "list"], (0, _winget_list_with_firefox(), "")),
             (["winget", "upgrade"], (0, WINGET_UPGRADE, ""))],
            winget=True, rows=[ROW_FIREFOX, ROW_VIRTUALBOX])
        names = {i["name"] for i in payload["items"]}
        self.assertNotIn("Mozilla Firefox (x64 en-US)", names)
        self.assertIn("Mozilla Firefox", names)
        self.assertIn(ROW_VIRTUALBOX["DisplayName"], names)
        # Firefox and App Installer from winget, VirtualBox from the registry.
        self.assertEqual(len(payload["items"]), 3)

    def test_user_hive_rows_are_user_scope(self):
        with patch.object(software, "_profile_list",
                          return_value={"S-1-5-21-1-2-3-1001": "alice"}):
            payload, _ = _collect([], rows=[ROW_ALICE])
        self.assertEqual(payload["errors"], {})
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["scope"], "user")
        self.assertEqual(item["user"], "alice")

    def test_user_hive_row_without_a_profile_falls_back_to_the_sid(self):
        with patch.object(software, "_profile_list", return_value={}):
            payload, _ = _collect([], rows=[ROW_ALICE])
        self.assertEqual(payload["items"][0]["user"], "S-1-5-21-1-2-3-1001")

    def test_wow64_duplicate_key_once(self):
        fake = _FakeWinreg({})
        rows: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for path in (software._UNINSTALL_64, software._UNINSTALL_WOW):
            handle = fake.OpenKey(fake.HKLM, path)
            for sub in fake.EnumKey(handle, 0), :
                software._read_uninstall_subkey(fake, handle, sub, "HKLM", "",
                                                rows, seen)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "Oracle.VirtualBox")
        self.assertEqual(rows[0]["DisplayName"],
                         "Oracle VirtualBox Guest Additions 7.2.18")

    def test_updates_and_hotfixes_skipped(self):
        rows = [ROW_VIRTUALBOX,
                dict(ROW_FIREFOX, key="KB5001234", DisplayName="Update One",
                     ReleaseType="Update"),
                dict(ROW_FIREFOX, key="KB5009999", DisplayName="Hotfix One",
                     ReleaseType="Hotfix"),
                dict(ROW_FIREFOX, key="KB5008888", DisplayName="Security One",
                     ReleaseType="Security Update"),
                dict(ROW_FIREFOX, key="SubFeature", DisplayName="Sub Feature",
                     ParentKeyName=ROW_VIRTUALBOX["key"])]
        payload, _ = _collect([], rows=rows)
        self.assertEqual([i["id"] for i in payload["items"]],
                         [ROW_VIRTUALBOX["key"]])


class NameKeyTests(unittest.TestCase):
    def test_name_key_matches_server(self):
        self.assertEqual(software.name_key("Mozilla Firefox (x64 en-US)"),
                         "mozilla firefox")
        self.assertEqual(software.name_key("7-Zip 23.01 (x64)"), "7-zip")
        self.assertEqual(software.name_key("Python 3.12.4 (64-bit)"), "python")
        self.assertEqual(software.name_key("Notepad++"), "notepad++")
        self.assertEqual(
            software.name_key("Microsoft Visual C++ 2015-2022 Redistributable"),
            "microsoft visual c++ 2015-2022 redistributable")


class CollectDispatchTests(unittest.TestCase):
    def test_collect_dispatches_by_platform(self):
        sentinel = {"digest": "x"}
        with patch.object(software, "sys") as fake_sys, \
                patch.object(software, "collect_windows",
                             return_value=sentinel) as windows, \
                patch.object(software, "collect_linux",
                             return_value=sentinel) as linux:
            fake_sys.platform = "win32"
            self.assertIs(software.collect(), sentinel)
            windows.assert_called_once_with()
            self.assertEqual(linux.call_count, 0)
            fake_sys.platform = "linux"
            self.assertIs(software.collect(), sentinel)
            self.assertEqual(windows.call_count, 1)
            linux.assert_called_once_with()


class FailureIsolationTests(unittest.TestCase):
    def test_failing_manager_is_an_error_not_a_crash(self):
        timeout = subprocess.TimeoutExpired(cmd=["winget", "list"], timeout=60)
        payload, _ = _collect(
            [(["winget", "list"], timeout),
             (["choco", "list"], (1, "", "boom\n")),
             (["cmd", "/c", "scoop", "export"], (0, SCOOP_EXPORT, ""))],
            winget=True, present=("choco", "scoop"), rows=[ROW_VIRTUALBOX])
        self.assertIn("winget", payload["errors"])
        self.assertIn("chocolatey", payload["errors"])
        self.assertEqual({i["source"] for i in payload["items"]},
                         {"scoop", "registry"})

    def test_unreadable_registry_is_an_error_not_a_crash(self):
        payload, _ = _collect([], rows_error=RuntimeError(
            "HKLM Uninstall key unreadable"))
        self.assertIn("registry", payload["errors"])
        self.assertEqual(payload["items"], [])

    def test_payload_shape_and_managed_flags(self):
        payload, _ = _collect(
            [(["winget", "list"], (0, WINGET_LIST, "")),
             (["winget", "upgrade"], (0, "", ""))],
            winget=True, rows=[ROW_VIRTUALBOX])
        self.assertEqual(sorted(payload),
                         ["collected_at", "digest", "errors", "items"])
        self.assertEqual(len(payload["digest"]), 64)
        for item in payload["items"]:
            self.assertEqual(sorted(item), sorted(software.ITEM_FIELDS))
        by_source = {i["source"]: i for i in payload["items"]}
        self.assertIs(by_source["winget"]["managed"], True)
        self.assertIs(by_source["registry"]["managed"], False)
        self.assertEqual(by_source["winget"]["scope"], "machine")
        self.assertEqual(by_source["winget"]["user"], "")


def _winget_list_with_firefox() -> str:
    """The VM capture's columns plus one winget-installed Firefox."""
    widths = (57, 82, 21, 10)            # Name, Id, Version, Available (from the VM header)

    def row(name, ident, version, available, source):
        return "".join(v.ljust(w) for v, w in zip((name, ident, version, available), widths)) + source

    header = row("Name", "Id", "Version", "Available", "Source")
    return "\n".join([
        header, "-" * len(header),
        row("Mozilla Firefox", "Mozilla.Firefox", "141.0", "142.0", "winget"),
        row("App Installer", "Microsoft.AppInstaller", "1.29.379.0", "1.29.380", "winget"),
    ]) + "\n"


class _FakeHandle:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """The slice of winreg the registry collector uses, for the WOW dedupe."""

    HKLM = "HKLM"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100

    _SUBKEYS = {
        software._UNINSTALL_64: ["Oracle.VirtualBox"],
        software._UNINSTALL_WOW: ["Oracle.VirtualBox"],
    }
    _VALUES = {"Oracle.VirtualBox": {
        "DisplayName": "Oracle VirtualBox Guest Additions 7.2.18",
        "DisplayVersion": "7.2.18.175117",
        "Publisher": "Oracle",
    }}

    def __init__(self, profiles):
        self.profiles = profiles

    def OpenKey(self, hkey, path, reserved=0, access=0):
        if path in self._SUBKEYS or path in self._VALUES or path in self.profiles:
            return _FakeHandle(path)
        raise FileNotFoundError(path)

    def EnumKey(self, handle, index):
        names = self._SUBKEYS.get(handle.name, [])
        if index < len(names):
            return names[index]
        raise OSError("no more data")

    def QueryValueEx(self, handle, name):
        values = self._VALUES.get(handle.name, self.profiles.get(handle.name, {}))
        if name in values:
            return values[name], 1
        raise FileNotFoundError(name)

    def CloseKey(self, handle):
        return None


if __name__ == "__main__":
    unittest.main()
