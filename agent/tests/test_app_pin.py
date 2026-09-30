"""``app_pin`` — what each source runs to hold or release an app.

Nothing here may run a real command: ``pkg_manager.detect``, ``pkg_manager._run``,
``apps._run_windows`` and ``software.collect_now`` are patched for every test and
the fakes record what they were asked to do. ``_run`` also answers dnf's
``versionlock --help``, which is how the handler learns the plugin is there.
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

_HOLD_HELP = "usage: dnf versionlock [-q] {add,delete,list} ..."
_NO_SUCH_COMMAND = ("No such command 'versionlock'. It could be the name of a "
                    "plugin. Try: dnf install python3-dnf-plugin-versionlock")

_WINGET = r"C:\winget\winget.exe"
_WINGET_ENV = {"PATH": r"C:\winget"}
#: winget's ``0x8A150063`` — the id has no pin — as the process reports it, and
#: as the unsigned HRESULT.
_NO_PIN_SIGNED = -1978335133
_NO_PIN_UNSIGNED = 2316632163


def _payload(*items):
    return {"items": [dict(item) for item in items]}


class _FakePM:
    """The detected manager: a name, and a version query of the host's own."""

    def __init__(self, name="apt-get", version=""):
        self.name = name
        self.version = version
        self.version_queries = []

    def installed_version(self, package_name):
        self.version_queries.append(package_name)
        return self.version


class _RecordingRun:
    """The substituted ``_run``: records argv, and fails or answers per token.

    Every argv is recorded, including the read-only ``versionlock --help``
    probe, so a test can assert that a refusal ran nothing at all.
    """

    def __init__(self, fail_for=(), no_versionlock=False):
        self.argvs = []
        self.fail_for = tuple(fail_for)
        self.no_versionlock = no_versionlock

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.argvs.append(argv)
        probe = "versionlock" in argv and "--help" in argv
        if not probe and any(token in argv for token in self.fail_for):
            raise RuntimeError(f"Command exited 1: {' '.join(argv)}")
        if probe:
            return _NO_SUCH_COMMAND if self.no_versionlock else _HOLD_HELP
        return ""

    @property
    def commands(self):
        return [argv for argv in self.argvs if "--help" not in argv]


class _RecordingWindowsRun:
    """The substituted ``_run_windows``: records argv and env, answers per code.

    The result is checked against the caller's own ``ok_codes`` — including the
    signed and unsigned spellings of a HRESULT — the way ``_check_exit`` does,
    so a test that hands back 0x8A150063 learns whether the action accepts it.
    """

    def __init__(self, result=(0, "")):
        self.calls = []
        self.result = result

    @property
    def commands(self):
        return [argv for argv, _env in self.calls]

    @property
    def envs(self):
        return [env for _argv, env in self.calls]

    def __call__(self, argv, env=None, **_kwargs):
        # Like the real apps._run_windows: return the exit code; judging it is
        # the handler's job (_check_exit), which is what these tests exercise.
        argv = list(argv)
        self.calls.append((argv, dict(env) if env else None))
        return self.result


class AppPinLinuxTests(unittest.TestCase):
    def setUp(self):
        self.pm = _FakePM()
        self.run = _RecordingRun()
        self.payloads = []
        # The handler reaches the host only through these: the detected
        # manager, the one command seam, and the collection.
        patchers = (mock.patch.object(pkg_manager, "detect", return_value=self.pm),
                    mock.patch.object(pkg_manager, "_run", side_effect=self.run),
                    mock.patch.object(software, "collect_now",
                                      side_effect=self._next_payload))
        self.detect, self.run_patch, self.collect = (p.start() for p in patchers)
        for patcher in patchers:
            self.addCleanup(patcher.stop)

    def _next_payload(self):
        return self.payloads.pop(0) if self.payloads else _payload()

    def use_pm(self, pm):
        self.pm = pm
        self.detect.return_value = pm

    # ── the commands ──────────────────────────────────────────────────────

    def test_linux_pin_commands(self):
        # (manager the host reports, the action's params, the argv it runs).
        # With no ``source`` the primary's own inventory name is used, which is
        # why apt's hold is asked of apt-mark and dnf's of versionlock — and
        # why a yum host is versionlocked by yum.
        cases = [
            ("apt-get", {"app": "openssl"}, [["apt-mark", "hold", "openssl"]]),
            ("apt-get", {"app": "openssl", "unpin": True},
             [["apt-mark", "unhold", "openssl"]]),
            ("dnf", {"app": "openssl"}, [["dnf", "versionlock", "add", "openssl"]]),
            ("dnf", {"app": "openssl", "unpin": True},
             [["dnf", "versionlock", "delete", "openssl"]]),
            ("yum", {"app": "openssl"}, [["yum", "versionlock", "add", "openssl"]]),
            ("yum", {"app": "openssl", "unpin": True},
             [["yum", "versionlock", "delete", "openssl"]]),
            ("zypper", {"app": "openssl"},
             [["zypper", "--non-interactive", "addlock", "openssl"]]),
            ("zypper", {"app": "openssl", "unpin": True},
             [["zypper", "--non-interactive", "removelock", "openssl"]]),
            ("apk", {"app": "openssl", "version": "2.14.10-r0"},
             [["apk", "add", "openssl=2.14.10-r0"]]),
            ("apk", {"app": "openssl", "unpin": True}, [["apk", "add", "openssl"]]),
            ("snapd", {"app": "firefox", "source": "snap"},
             [["snap", "refresh", "--hold=forever", "firefox"]]),
            ("snapd", {"app": "firefox", "source": "snap", "unpin": True},
             [["snap", "refresh", "--unhold", "firefox"]]),
            ("flatpak", {"app": "firefox", "source": "flatpak"},
             [["flatpak", "mask", "firefox"]]),
            ("flatpak", {"app": "firefox", "source": "flatpak", "unpin": True},
             [["flatpak", "mask", "--remove", "firefox"]]),
        ]
        for name, params, expected in cases:
            with self.subTest(manager=name, params=sorted(params)):
                self.run.argvs = []
                self.use_pm(_FakePM(name))
                out = apps._app_pin(params, _CONFIG)
                self.assertEqual(self.run.commands, expected)
                self.assertIs(out.data["pinned"], not params.get("unpin", False))

    def test_apt_at_version_installs_then_holds_in_that_order(self):
        out = apps._app_pin({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(self.run.commands, [
            ["apt-get", "install", "-y", "-qq", "openssl=3.0.13-1"],
            ["apt-mark", "hold", "openssl"],
        ])
        self.assertEqual(out.data["pinned_version"], "3.0.13-1")

        self.use_pm(_FakePM("dnf"))
        self.run.argvs = []
        apps._app_pin({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(self.run.commands, [
            ["dnf", "install", "-y", "--quiet", "openssl-3.0.13-1"],
            ["dnf", "versionlock", "add", "openssl"],
        ])

        self.use_pm(_FakePM("zypper"))
        self.run.argvs = []
        apps._app_pin({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(self.run.commands, [
            ["zypper", "--non-interactive", "install", "-y", "openssl=3.0.13-1"],
            ["zypper", "--non-interactive", "addlock", "openssl"],
        ])

    def test_apk_without_a_version_freezes_the_hosts_own(self):
        self.use_pm(_FakePM("apk", version="2.14.10-r0"))
        out = apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.pm.version_queries, ["openssl"])
        self.assertEqual(self.run.commands, [["apk", "add", "openssl=2.14.10-r0"]])
        self.assertEqual(out.data["pinned_version"], "2.14.10-r0")

        self.use_pm(_FakePM("apk"))
        self.run.argvs = []
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertIn("apk pins by version", str(ctx.exception))
        self.assertEqual(self.run.commands, [])

    def test_dnf_versionlock_missing_message(self):
        self.use_pm(_FakePM("dnf"))
        self.run = _RecordingRun(no_versionlock=True)
        self.run_patch.side_effect = self.run
        for params in ({"app": "openssl"}, {"app": "openssl", "unpin": True}):
            with self.subTest(unpin=params.get("unpin", False)):
                self.run.argvs = []
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin(params, _CONFIG)
                self.assertEqual(
                    "dnf versionlock plugin missing — install "
                    "python3-dnf-plugin-versionlock", str(ctx.exception))
                self.assertEqual(self.run.commands, [],
                                 "a host without the plugin ran no lock command")

    def test_a_real_command_failure_is_not_the_plugin_message(self):
        self.use_pm(_FakePM("dnf"))
        self.run.fail_for = ("add",)
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertNotIn("plugin missing", str(ctx.exception))
        self.assertIn("versionlock", str(ctx.exception))

    # ── refusals ──────────────────────────────────────────────────────────

    def test_refusals(self):
        self.use_pm(_FakePM("pacman"))
        for params in ({"app": "openssl"}, {"app": "openssl", "version": "1.2"}):
            with self.subTest(params=sorted(params)):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin(params, _CONFIG)
                self.assertIn("pacman holds live in pacman.conf IgnorePkg — edit "
                              "it by hand", str(ctx.exception))

        self.use_pm(_FakePM("apt-get"))
        self.run.argvs = []
        for store in ("snap", "flatpak"):
            with self.subTest(store=store):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin({"app": "firefox", "source": store,
                                   "version": "125"}, _CONFIG)
                self.assertIn(f"{store} cannot pin a version", str(ctx.exception))
        self.assertEqual(self.run.argvs, [], "a refused pin ran no command")

    def test_a_windows_only_source_is_refused_on_linux(self):
        # scoop and the registry have refusal messages of their own; they are
        # asserted on the Windows side, where their sources resolve at all.
        self.use_pm(_FakePM("dnf"))
        for source in ("scoop", "registry"):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin({"app": "7zip", "source": source}, _CONFIG)
                self.assertEqual(f"source {source} is Windows-only",
                                 str(ctx.exception))
        self.assertEqual(self.run.argvs, [])

    def test_bad_params_refused_before_any_command(self):
        for params in ({"app": "openssl", "unpin": "yes"},
                       {"app": "openssl", "unpin": 1},
                       {"app": "openssl", "unpin": True, "version": "3.0.13-1"},
                       {"app": "openssl", "source": "apt"},
                       {"app": "foo;rm -rf /"},
                       {"app": "openssl", "version": "3.0 13"}):
            with self.subTest(params=sorted(params)), \
                    self.assertRaises(RuntimeError):
                apps._app_pin(params, _CONFIG)
        self.assertEqual(self.run.argvs, [])
        self.assertEqual(self.collect.call_count, 0,
                         "a refused pin did not re-collect")

    # ── outputs ───────────────────────────────────────────────────────────

    def test_outputs(self):
        self.payloads = [_payload({"source": "dpkg", "id": "openssl",
                                   "version": "3.0.13-1"})]
        out = apps._app_pin({"app": "openssl", "version": "3.0.13-1"}, _CONFIG)
        self.assertEqual(out.data, {"pinned": True,
                                    "pinned_version": "3.0.13-1"})

        # The collection is the authority on what the app is at now.
        self.payloads = [_payload({"source": "dpkg", "id": "openssl",
                                   "version": "3.0.13-2"})]
        out = apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data, {"pinned": True,
                                    "pinned_version": "3.0.13-2"})

        # An unpin reports the version it left behind, with pinned False.
        self.payloads = [_payload({"source": "dpkg", "id": "openssl",
                                   "version": "3.0.13-2"})]
        out = apps._app_pin({"app": "openssl", "unpin": True}, _CONFIG)
        self.assertEqual(out.data, {"pinned": False,
                                    "pinned_version": "3.0.13-2"})

        # Nothing in the collection and no version asked: empty, not absent.
        self.payloads = [_payload()]
        out = apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertEqual(out.data, {"pinned": True, "pinned_version": ""})

    def test_recollect_after_success_not_after_failure(self):
        apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.collect.call_count, 1)

        self.run.fail_for = ("hold",)
        with self.assertRaises(RuntimeError):
            apps._app_pin({"app": "openssl"}, _CONFIG)
        self.assertEqual(self.collect.call_count, 1,
                         "a failed command must not re-collect")

    def test_handler_is_wired_and_allowlistable(self):
        self.assertIs(executor._HANDLERS["app_pin"], apps._app_pin)
        self.assertIn("app_pin", _ALL_ACTIONS)


class AppPinWindowsTests(unittest.TestCase):
    """winget's ``pin`` argv and its measured exit codes, and Chocolatey's."""

    def setUp(self):
        self.run = _RecordingWindowsRun()
        self.payloads = []
        patchers = (
            mock.patch.object(apps, "_run_windows", side_effect=self.run),
            mock.patch.object(apps.sys, "platform", "win32"),
            # sys.platform is patched process-wide; the real shutil.which would
            # then take its Windows path on this POSIX host.
            mock.patch.object(apps.shutil, "which", return_value=None),
            mock.patch.object(pkg_manager, "resolve_winget",
                              return_value=(_WINGET, "")),
            mock.patch.object(pkg_manager, "winget_env",
                              side_effect=lambda _b: dict(_WINGET_ENV)),
            mock.patch.object(software, "collect_now",
                              side_effect=self._next_payload),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _next_payload(self):
        return self.payloads.pop(0) if self.payloads else _payload()

    def test_windows_pin_commands_and_codes(self):
        out = apps._app_pin({"app": "Microsoft.Edge", "source": "winget"},
                            _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "pin", "add", "--id", "Microsoft.Edge", "--exact",
            "--blocking", "--accept-source-agreements",
            "--disable-interactivity"]])
        self.assertEqual(self.run.envs[-1], _WINGET_ENV)
        self.assertIs(out.data["pinned"], True)

        self.run.calls = []
        apps._app_pin({"app": "Microsoft.Edge", "source": "winget",
                       "version": "140.0.3485.81"}, _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "pin", "add", "--id", "Microsoft.Edge", "--exact",
            "--blocking", "--accept-source-agreements",
            "--disable-interactivity", "--version", "140.0.3485.81"]])

        self.run.calls = []
        out = apps._app_pin({"app": "Microsoft.Edge", "source": "winget",
                             "unpin": True}, _CONFIG)
        self.assertEqual(self.run.commands, [[
            _WINGET, "pin", "remove", "--id", "Microsoft.Edge", "--exact",
            "--disable-interactivity"]])
        self.assertIs(out.data["pinned"], False)

        # Measured on the Windows VM: ``pin remove`` on an id with no pin exits
        # 0x8A150063, either way round — the app may upgrade in both cases.
        for code in (_NO_PIN_SIGNED, _NO_PIN_UNSIGNED):
            with self.subTest(code=code):
                self.run.calls = []
                self.run.result = (code, "")
                out = apps._app_pin({"app": "Microsoft.Edge", "source": "winget",
                                     "unpin": True}, _CONFIG)
                self.assertIs(out.data["pinned"], False)

        # ``pin add`` is not forgiving of it: that code means there is no pin,
        # which is exactly the failure of the command that asked for one.
        self.run.calls = []
        self.run.result = (_NO_PIN_SIGNED, "")
        with self.assertRaises(RuntimeError) as ctx:
            apps._app_pin({"app": "Microsoft.Edge", "source": "winget"}, _CONFIG)
        self.assertIn(f"exited {_NO_PIN_SIGNED}", str(ctx.exception))

    def test_choco_pin_commands(self):
        apps._app_pin({"app": "7zip", "source": "chocolatey"}, _CONFIG)
        self.assertEqual(self.run.commands, [["choco", "pin", "add", "-n=7zip"]])

        self.run.calls = []
        apps._app_pin({"app": "7zip", "source": "chocolatey",
                       "version": "25.1.0"}, _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "pin", "add", "-n=7zip",
                           "--version=25.1.0"]])

        self.run.calls = []
        apps._app_pin({"app": "7zip", "source": "chocolatey", "unpin": True},
                      _CONFIG)
        self.assertEqual(self.run.commands,
                         [["choco", "pin", "remove", "-n=7zip"]])

    def test_windows_refusals(self):
        for source, expected in (("pacman", "pacman holds live in pacman.conf"),
                                 ("scoop", "scoop cannot be pinned"),
                                 ("registry", "registry cannot be pinned")):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin({"app": "7zip", "source": source}, _CONFIG)
                self.assertIn(expected, str(ctx.exception))
        for source in ("snap", "flatpak"):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError) as ctx:
                    apps._app_pin({"app": "firefox", "source": source,
                                   "version": "125"}, _CONFIG)
                self.assertIn("is Linux-only", str(ctx.exception))
        self.assertEqual(self.run.commands, [])

    def test_windows_outputs(self):
        self.payloads = [_payload({"source": "winget", "id": "Microsoft.Edge",
                                   "version": "140.0.3485.81"})]
        out = apps._app_pin({"app": "Microsoft.Edge", "source": "winget",
                             "version": "140.0.3485.0"}, _CONFIG)
        self.assertEqual(out.data, {"pinned": True,
                                    "pinned_version": "140.0.3485.81"})

        self.payloads = [_payload()]
        out = apps._app_pin({"app": "Microsoft.Edge", "source": "winget"}, _CONFIG)
        self.assertEqual(out.data, {"pinned": True, "pinned_version": ""})


if __name__ == "__main__":
    unittest.main()
