"""The Windows side of the app actions — argv, winget's exit codes, the registry.

Nothing here may run a real command: ``apps._run_windows`` is the single seam
the whole Windows half goes through and every test substitutes it, together with
``pkg_manager.resolve_winget`` / ``winget_env``, ``software._read_uninstall_keys``
and ``software.collect_now``, and ``sys.platform`` so the handlers take their
Windows branch. The MSI and VirtualBox rows are the ones measured on the VM.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
# executor first: the actions modules import from it, and it imports their
# handlers — importing actions.apps first would enter that cycle half-built.
from vigil_agent import executor, pkg_manager, software  # noqa: F401
from vigil_agent.actions import apps
from vigil_agent.config import AgentConfig

_CONFIG = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40)

_WINGET = (r"C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_1.28. "
           r"291.0_x64__8wekyb3d8bbwe\winget.exe")
#: What ``winget_env`` returns for the resolver's binary; the tests assert the
#: commands run with exactly this environment.
_WINGET_ENV = {"PATH": r"C:\Windows\System32;VCLibsRuntime", "SYSTEMROOT":
               r"C:\Windows"}

# The VM's own rows, as _read_uninstall_keys returns them (phase 03's capture).
# The GUID is the VM's Python launcher MSI product code, as the docs record it;
# the key name here is machine-readable rather than the bare code, so the msiexec
# path is exercised both ways (key is a code / GUID from the uninstall string).
_MSI_ROW = {
    "hive": "HKLM", "sid": "", "key": "Python.Launcher",
    "DisplayName": "Python 3.12.10 Executable Launchers (64-bit)",
    "DisplayVersion": "3.12.10150.0", "Publisher": "Python Software Foundation",
    "SystemComponent": None, "WindowsInstaller": 1, "QuietUninstallString": None,
    "UninstallString": "MsiExec.exe /I{0158093D-F809-455B-9429-6D16A4B5D118}",
}
_VBOX_ROW = {
    "hive": "HKLM", "sid": "", "key": "Oracle VirtualBox Guest Additions",
    "DisplayName": "Oracle VirtualBox Guest Additions 7.2.18",
    "DisplayVersion": "7.2.18.175117",
    "Publisher": "Oracle and/or its affiliates", "SystemComponent": None,
    "WindowsInstaller": None, "QuietUninstallString": None,
    "UninstallString": r"C:\Program Files\Oracle\VirtualBox Guest Additions"
                       r"\uninst.exe",
}
_QUIET_ROW = {
    "hive": "HKLM", "sid": "", "key": "Git_is1", "DisplayName": "Git",
    "DisplayVersion": "2.51.0", "Publisher": "The Git Development Community",
    "SystemComponent": None, "WindowsInstaller": None,
    "QuietUninstallString": r'C:\Program Files\Git\unins000.exe /SILENT',
    "UninstallString": r'C:\Program Files\Git\unins000.exe',
}
_HKU_ROW = {
    "hive": "HKU", "sid": "S-1-5-21-3200555555-206275973-3752638982-1001",
    "key": "Cursor", "DisplayName": "Cursor", "DisplayVersion": "1.7.52",
    "Publisher": "Anysphere", "SystemComponent": None, "WindowsInstaller": None,
    "QuietUninstallString": r"C:\Users\vigil\AppData\Local\Programs\cursor"
                            r"\unins000.exe /SILENT",
    "UninstallString": r"C:\Users\vigil\AppData\Local\Programs\cursor"
                       r"\unins000.exe",
}


def _payload(*items):
    return {"items": [dict(item) for item in items]}


class _RecordingRun:
    """The substituted ``_run_windows``: records what it was asked to run."""

    def __init__(self, result=(0, "")):
        self.calls = []
        self.result = result

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if isinstance(self.result, list):
            return self.result.pop(0)
        return self.result

    @property
    def commands(self):
        return [command for command, _kwargs in self.calls]

    def env_for(self, index: int = 0):
        return self.calls[index][1].get("env")

    def timeout_for(self, index: int = 0):
        return self.calls[index][1].get("timeout")


class WindowsAppActionTests(unittest.TestCase):
    def setUp(self):
        self.run = _RecordingRun()
        self.rows = []
        self.payloads = []
        # The handlers reach the host only through these: the one command seam,
        # the winget resolver and its env, the registry rows, the collection.
        patchers = (
            mock.patch.object(apps, "_run_windows", side_effect=self.run),
            mock.patch.object(apps.sys, "platform", "win32"),
            # sys.platform is patched process-wide; the real shutil.which would
            # then take its Windows path on this POSIX host. Tests that need a
            # tool present patch it back themselves.
            mock.patch.object(apps.shutil, "which", return_value=None),
            mock.patch.object(pkg_manager, "resolve_winget",
                              return_value=(_WINGET, "")),
            mock.patch.object(pkg_manager, "winget_env",
                              side_effect=lambda b: dict(_WINGET_ENV)),
            mock.patch.object(software, "_read_uninstall_keys",
                              side_effect=lambda: [dict(r) for r in self.rows]),
            mock.patch.object(software, "collect_now",
                              side_effect=self._next_payload),
        )
        started = [p.start() for p in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
        self.run_patch, self.platform, self.which, self.resolve, self.winget_env = started[:5]

    def _next_payload(self):
        return self.payloads.pop(0) if self.payloads else _payload()

    def fail_with(self, code, output=""):
        """The next command exits *code* with winget/choco's own *output*."""
        self.run.result = (code, output)

    # ── winget ────────────────────────────────────────────────────────────

    def test_winget_argv_and_env(self):
        self.payloads = [_payload({"source": "winget", "id": "Microsoft.Edge",
                                   "version": "140.0.3485.81"})]
        out = apps._app_install({"app": "Microsoft.Edge", "source": "winget",
                                 "version": "140.0.3485.81"}, _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "install", "--id", "Microsoft.Edge", "--exact", "--silent",
            "--scope", "machine", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity",
            "--version", "140.0.3485.81"]])
        self.assertEqual(self.run.env_for(), _WINGET_ENV)
        self.assertEqual(self.run.timeout_for(), 900)
        self.assertEqual(out.data, {"installed_version": "140.0.3485.81",
                                    "source": "winget"})

        self.run.calls = []
        apps._app_upgrade({"app": "Python.Python.3.12", "source": "winget"},
                          _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "upgrade", "--id", "Python.Python.3.12", "--exact",
            "--silent", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity"]])
        self.assertEqual(self.run.env_for(), _WINGET_ENV)

        self.run.calls = []
        apps._app_upgrade({"source": "winget"}, _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "upgrade", "--all", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity"]])

        self.run.calls = []
        apps._app_uninstall({"app": "Microsoft.Edge", "source": "winget"},
                            _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "uninstall", "--id", "Microsoft.Edge", "--exact",
            "--silent", "--disable-interactivity"]])
        self.assertEqual(self.run.env_for(), _WINGET_ENV)

    def test_winget_benign_exit_codes(self):
        # 0x8A15002B: "No applicable update found" — the app is current.
        self.fail_with(-1978335189, "No applicable update found.")
        out = apps._app_upgrade({"app": "Microsoft.Edge", "source": "winget"},
                                _CONFIG)
        self.assertEqual(out.data, {"upgraded": 0, "failed": 0})

        # The same HRESULT reaching us as its unsigned form.
        self.fail_with(2316632107, "No applicable update found.")
        out = apps._app_upgrade({"app": "Microsoft.Edge", "source": "winget"},
                                _CONFIG)
        self.assertEqual(out.data, {"upgraded": 0, "failed": 0})

        # 0x8A150061: "Package already installed" — the install succeeded.
        self.payloads = [_payload({"source": "winget", "id": "Microsoft.Edge",
                                   "version": "140.0.3485.81"})]
        self.fail_with(-1978335135, "Package already installed.")
        out = apps._app_install({"app": "Microsoft.Edge", "source": "winget",
                                 "version": "140.0.3485.81"}, _CONFIG)
        self.assertEqual(out.data, {"installed_version": "140.0.3485.81",
                                    "source": "winget"})

        # Anything else is a failure that names winget's own last line.
        self.fail_with(5, "The server was unable to process the request.")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_install({"app": "Microsoft.Edge", "source": "winget"},
                              _CONFIG)
        self.assertIn("winget exited 5", str(ctx.exception))

    def test_winget_real_failures_surface_wingets_message(self):
        # Measured on the VM: Edge refuses an upgrade whose install technology
        # differs, and an unknown id is not a success.
        self.fail_with(-1978335090,
                       "A newer version was found, but the install technology "
                       "is different from the current version installed")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_upgrade({"app": "Microsoft.Edge", "source": "winget"},
                              _CONFIG)
        self.assertIn("winget exited -1978335090", str(ctx.exception))
        self.assertIn("install technology is different", str(ctx.exception))

        self.fail_with(-1978335212,
                       "No installed package found matching input criteria.")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "Not.A.Package", "source": "winget"},
                                _CONFIG)
        self.assertIn("No installed package found matching input criteria",
                      str(ctx.exception))

    # ── Chocolatey ────────────────────────────────────────────────────────

    def test_choco_commands_and_reboot_codes(self):
        self.payloads = [_payload({"source": "chocolatey", "id": "7zip",
                                   "version": "25.1.0"})]
        out = apps._app_install({"app": "7zip", "source": "chocolatey",
                                 "version": "25.0.1"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "install", "7zip", "-y", "--no-progress",
                           "--version", "25.0.1"]])
        self.assertEqual(out.data, {"installed_version": "25.1.0",
                                    "source": "chocolatey"})

        self.run.calls = []
        apps._app_upgrade({"app": "7zip", "source": "chocolatey"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "upgrade", "7zip", "-y", "--no-progress"]])

        self.run.calls = []
        apps._app_upgrade({"source": "chocolatey"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "upgrade", "all", "-y", "--no-progress"]])

        self.run.calls = []
        apps._app_uninstall({"app": "7zip", "source": "chocolatey"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "uninstall", "7zip", "-y"]])

        # 3010: the removal happened; something is still to be done at reboot.
        self.run.calls = []
        self.fail_with(3010, "Restart required to complete the operation.")
        out = apps._app_uninstall({"app": "7zip", "source": "chocolatey"},
                                  _CONFIG)
        self.assertIs(out.data["removed"], True)
        self.assertIn("reboot", str(out))

        self.run.calls = []
        self.fail_with(1641, "")
        out = apps._app_upgrade({"app": "7zip", "source": "chocolatey"},
                                _CONFIG)
        self.assertEqual(out.data, {"upgraded": 1, "failed": 0})
        self.assertIn("reboot", str(out))

        self.fail_with(1, "Chocolatey detected you are not running from an "
                          "elevated command prompt")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_install({"app": "7zip", "source": "chocolatey"}, _CONFIG)
        self.assertIn("choco exited 1", str(ctx.exception))

    # ── registry uninstall ────────────────────────────────────────────────

    def test_registry_msi_uninstall(self):
        self.rows = [_MSI_ROW]
        guid = "{0158093D-F809-455B-9429-6D16A4B5D118}"
        out = apps._app_uninstall({"app": "Python.Launcher",
                                   "source": "registry"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["msiexec.exe", "/x", guid, "/qn", "/norestart"]])
        self.assertEqual(self.run.timeout_for(), 900)
        self.assertIs(out.data["removed"], True)

        # A row whose key *is* the product code (the VM's Python MSI parts)
        # uninstalls by the key, without looking at the uninstall string.
        self.rows = [dict(_MSI_ROW, key=guid, SystemComponent=1)]
        self.run.calls = []
        apps._app_uninstall({"app": guid, "source": "registry"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["msiexec.exe", "/x", guid, "/qn", "/norestart"]])

        # 1605: "This product is not installed" — nothing to remove, and the
        # re-collection is what says whether the host is rid of it.
        self.rows = [_MSI_ROW]
        self.run.calls = []
        self.fail_with(1605, "The product is already installed.")
        out = apps._app_uninstall({"app": "Python.Launcher",
                                   "source": "registry"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["msiexec.exe", "/x", guid, "/qn", "/norestart"]])
        self.assertIs(out.data["removed"], True)

        # A real failure is msiexec's, with its own words.
        self.fail_with(1603, "Error 1603: Error during installation.")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "Python.Launcher",
                                 "source": "registry"}, _CONFIG)
        self.assertIn("msiexec exited 1603", str(ctx.exception))
        self.assertIn("Error during installation", str(ctx.exception))

    def test_registry_quiet_string_and_refusals(self):
        self.rows = [_QUIET_ROW]
        out = apps._app_uninstall({"app": "Git_is1", "source": "registry"},
                                  _CONFIG)
        # The string, not a list and not through a shell.
        self.assertEqual(self.run.commands,
                         [_QUIET_ROW["QuietUninstallString"]])
        self.assertIs(self.run.calls[0][1].get("shell", False), False)
        self.assertIs(out.data["removed"], True)

        # The VM's VirtualBox row: an interactive uninst.exe and nothing quiet.
        self.rows = [_VBOX_ROW]
        self.run.calls = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "Oracle VirtualBox Guest Additions",
                                 "source": "registry"}, _CONFIG)
        self.assertIn("has no silent uninstaller (only an interactive "
                      "UninstallString)", str(ctx.exception))
        self.assertEqual(self.run.commands, [],
                         "an interactive uninstaller was not run")

        # Per-user installs are left to their owners.
        self.rows = [_HKU_ROW]
        self.run.calls = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "Cursor", "source": "registry"},
                                _CONFIG)
        self.assertIn("per-user install; Vigil does not uninstall per-user apps",
                      str(ctx.exception))
        self.assertEqual(self.run.commands, [])

        self.rows = [_MSI_ROW]
        self.run.calls = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_uninstall({"app": "Nowhere", "source": "registry"},
                                _CONFIG)
        self.assertEqual("no machine-wide Uninstall entry Nowhere",
                         str(ctx.exception))
        self.assertEqual(self.run.commands, [])

    # ── source resolution ─────────────────────────────────────────────────

    def test_source_resolution_windows(self):
        self.payloads = [_payload({"source": "winget", "id": "7zip",
                                   "version": "25.1.0"})]
        out = apps._app_install({"app": "7zip"}, _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "install", "--id", "7zip", "--exact", "--silent", "--scope",
            "machine", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity"]])
        self.assertEqual(out.data["source"], "winget")

        # winget absent (Server 2019 / LTSC), Chocolatey present.
        self.run.calls = []
        self.resolve.return_value = (None, "App Installer not found")
        self.which.side_effect = lambda name: "C:/ProgramData/chocolatey/bin/choco.exe" if name == "choco" else None
        out = apps._app_install({"app": "7zip"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "install", "7zip", "-y", "--no-progress"]])
        self.assertEqual(out.data["source"], "chocolatey")

        for source in ("dpkg", "rpm", "apk", "pacman"):
            with self.subTest(source=source):
                self.run.calls = []
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_install({"app": "openssl", "source": source},
                                      _CONFIG)
                self.assertEqual(f"source {source} is Linux-only",
                                 str(ctx.exception))
                self.assertEqual(self.run.commands, [])

        self.run.calls = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_install({"app": "cursor", "source": "scoop"}, _CONFIG)
        self.assertEqual("Scoop installs are per-user; Vigil does not manage "
                         "them in this release", str(ctx.exception))
        self.assertEqual(self.run.commands, [])

        # Neither manager: the refusal names both.
        self.resolve.return_value = (None, "App Installer not found")
        with mock.patch.object(apps.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                apps._app_install({"app": "7zip"}, _CONFIG)
            self.assertEqual("no package manager on this host (winget / "
                             "Chocolatey)", str(ctx.exception))
        self.assertEqual(self.run.commands, [])

        # The registry is not a source anything installs from or upgrades to.
        self.run.calls = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_upgrade({"app": "Git_is1", "source": "registry"}, _CONFIG)
        self.assertIn("no package manager can upgrade it", str(ctx.exception))
        self.assertEqual(self.run.commands, [])


if __name__ == "__main__":
    unittest.main()
