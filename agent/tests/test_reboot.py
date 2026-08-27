"""Reboot action: platform split, message sanitizing, deferral state, the
collector's reboot_required probe, and the check-in wiring (phases 08a/08c).

The Windows argv is asserted here by test only; it has not run on Windows.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import client, collector, executor
from vigil_agent.config import AgentConfig
from vigil_agent.deferral import RebootDeferral

# The exact character set _sanitize_notify_message keeps.
ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9 .,:;!?()\-'']*$")


def _config(tmp: str) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="full_control",
        data_dir=Path(tmp),
    )


class RebootArgvTests(unittest.TestCase):
    """The platform split: /r /t <seconds> on Windows, -r now / +<minutes>
    everywhere else. _run is mocked — no real shutdown ever runs."""

    def _argv(self, params, platform, tmpdir=None):
        seen = {}

        def fake_run(cmd, timeout=None):
            seen["cmd"] = cmd
            return "ok"

        tmp = tempfile.gettempdir() if tmpdir is None else tmpdir
        with patch.object(sys, "platform", platform), \
                patch.object(executor, "_run", fake_run):
            executor._reboot(dict(params), _config(tmp))
        return seen["cmd"]

    def test_windows_uses_slash_t(self):
        argv = self._argv({"delay_seconds": 0}, "win32")
        # /t takes raw seconds; /f forces apps closed when no deferral
        # is in play (no defer_limit, no task_id here).
        self.assertEqual(argv, ["shutdown", "/r", "/t", "0", "/f"])
        self.assertNotIn("now", argv)
        self.assertFalse(any(a.startswith("+") for a in argv))

    def test_windows_delay_is_raw_seconds(self):
        argv = self._argv({"delay_seconds": 300}, "win32")
        self.assertEqual(argv[:4], ["shutdown", "/r", "/t", "300"])
        self.assertNotIn("now", argv)
        self.assertFalse(any(a.startswith("+") for a in argv))

    def test_windows_message_becomes_c(self):
        argv = self._argv(
            {"delay_seconds": 0, "notify_message": "patching"}, "win32")
        self.assertEqual(argv,
                         ["shutdown", "/r", "/t", "0", "/c", "patching", "/f"])

    def test_linux_uses_plus_minutes(self):
        # -r +N takes MINUTES: 300 s must not become +300.
        self.assertEqual(
            self._argv({"delay_seconds": 300}, "linux"),
            ["shutdown", "-r", "+5"])

    def test_linux_immediate_unchanged(self):
        # Pins the pre-existing coreutils behaviour byte-for-byte.
        self.assertEqual(
            self._argv({"delay_seconds": 0}, "linux"),
            ["shutdown", "-r", "now"])


class NotifyMessageSanitiseTests(unittest.TestCase):
    """The message comes from the server and lands on a command line; only
    the conservative allowlist may survive."""

    def test_notify_message_is_sanitised(self):
        for raw in ('; shutdown /s',
                    '" & del C:\\',
                    'a\nb',
                    "x" * 500):
            with self.subTest(raw=raw[:20]):
                out = executor._sanitize_notify_message(raw)
                self.assertLessEqual(len(out), 200)
                self.assertRegex(out, ALLOWED_CHARS)

    def test_sanitise_caps_at_200(self):
        out = executor._sanitize_notify_message("a" * 500)
        self.assertEqual(len(out), 200)

    def test_sanitise_keeps_allowlisted_text(self):
        out = executor._sanitize_notify_message("Patch! Reboot at 3pm (1).")
        self.assertEqual(out, "Patch! Reboot at 3pm (1).")

    def test_sanitise_drops_quotes_newlines_and_backslashes(self):
        out = executor._sanitize_notify_message('" & \na')
        for ch in ('"', "\n", "\\"):
            self.assertNotIn(ch, out)


class DeferralStateTests(unittest.TestCase):
    """Deferral state persists beside the agent's other state and counts
    down on a wall-clock deadline that survives an agent restart."""

    def test_deferral_state_persists_and_counts_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1 = RebootDeferral(Path(tmp))
            d1.record("task-1", defer_limit=3, defer_minutes=30)
            d1.defer(30)

            # A fresh instance over the same dir reloads the same state.
            d2 = RebootDeferral(Path(tmp))
            self.assertGreater(d2.expiry_remaining(), 1795)
            self.assertLessEqual(d2.expiry_remaining(), 1800)

            data = json.loads((Path(tmp) / "reboot_deferral.json").read_text())
            self.assertEqual(data["task_id"], "task-1")
            self.assertEqual(data["limit"], 3)
            self.assertEqual(data["used"], 1)

            st = (Path(tmp) / "reboot_deferral.json").stat()
            self.assertEqual(st.st_mode & 0o777, 0o600)

            # A different task id resets the budget; the same id keeps it.
            d2.record("task-2", defer_limit=2, defer_minutes=15)
            self.assertEqual(d2.expiry_remaining(), 0.0)
            d2.defer(15)
            d2.record("task-2", defer_limit=2, defer_minutes=15)
            self.assertGreater(d2.expiry_remaining(), 0.0)

    def test_exhausted_only_when_used_up_and_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = RebootDeferral(Path(tmp))
            d.record("task-1", defer_limit=1, defer_minutes=5)
            self.assertFalse(d.exhausted())
            d.defer(5)
            self.assertFalse(d.exhausted())
            # _expires_at is a monotonic deadline; 0.0 means the window
            # has elapsed, and used == limit now, so the budget is gone.
            d._expires_at = 0.0
            self.assertTrue(d.exhausted())


class RebootDeferralFlowTests(unittest.TestCase):
    """_reboot against the persistent deferral state: an active deferral
    withholds /f, an exhausted one reboots, and a failed notification never
    blocks the reboot."""

    def _run_reboot(self, params, platform, tmp):
        seen = []

        def fake_run(cmd, timeout=None):
            seen.append(cmd)
            return "ok"

        with patch.object(sys, "platform", platform), \
                patch.object(executor, "_run", fake_run):
            executor._reboot(dict(params), _config(tmp))
        return seen

    def test_active_deferral_withholds_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            RebootDeferral(Path(tmp)).record(
                "task-1", defer_limit=3, defer_minutes=30)
            RebootDeferral(Path(tmp)).defer(30)
            argv = self._run_reboot(
                {"task_id": "task-1", "defer_limit": 3,
                 "defer_minutes": 30, "delay_seconds": 0},
                "win32", tmp)
        self.assertEqual(argv[0], ["shutdown", "/r", "/t", "0"])
        self.assertNotIn("/f", argv[0])

    def test_deferral_limit_exhausted_reboots(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = RebootDeferral(Path(tmp))
            d.record("task-1", defer_limit=1, defer_minutes=5)
            d.defer(5)
            # The pending window has elapsed — persist it so _reboot's
            # fresh instance over the same dir sees the expired state.
            d._expires_at = 0.0
            d._save()
            argv = self._run_reboot(
                {"task_id": "task-1", "defer_limit": 1,
                 "defer_minutes": 5, "delay_seconds": 0},
                "win32", tmp)
        self.assertEqual(argv[0], ["shutdown", "/r", "/t", "0", "/f"])
        self.assertFalse(
            (Path(tmp) / "reboot_deferral.json").exists())

    def test_notification_failure_does_not_block_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def flaky_run(cmd, timeout=None):
                calls.append(cmd)
                if cmd[0] in ("notify-send", "msg", "osascript", "wall"):
                    raise RuntimeError("no desktop session")
                return "ok"

            with patch.object(sys, "platform", "linux"), \
                    patch.object(executor, "_run", flaky_run):
                out = executor._reboot(
                    {"delay_seconds": 0, "notify": True,
                     "notify_message": "patching"}, _config(tmp))
            self.assertEqual(out, "ok")
            self.assertEqual(calls[-1], ["shutdown", "-r", "now"])

    def test_invalid_defer_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(executor, "_run"):
            with self.assertRaises(ValueError):
                executor._reboot({"defer_limit": 9}, _config(tmp))


class RebootRequiredProbeTests(unittest.TestCase):
    """A probe error must never be reported as "needs reboot" and must never
    raise into the collector and break a check-in."""

    def _linux_only(self):
        if sys.platform == "win32":
            self.skipTest("probe paths are platform-specific")

    def test_reboot_required_probe_never_raises(self):
        self._linux_only()
        # Linux path: marker check and dnf both raise — result must be
        # False, and nothing may escape.
        with patch.object(sys, "platform", "linux"), \
                patch("subprocess.run", side_effect=OSError("no dnf binary")), \
                patch.object(Path, "exists",
                              side_effect=PermissionError("locked down")):
            self.assertFalse(collector.reboot_required())
        # Windows path: the winreg probe itself raises (winreg does not
        # exist off-Windows, so this also proves no winreg import at module
        # level) — result must be False.
        with patch.object(sys, "platform", "win32"), \
                patch.object(collector, "_reboot_required_windows",
                             side_effect=ImportError("no winreg on linux")):
            self.assertFalse(collector.reboot_required())
        # macOS: always False.
        with patch.object(sys, "platform", "darwin"):
            self.assertFalse(collector.reboot_required())

    def test_reboot_required_true_when_marker_present(self):
        self._linux_only()

        def fake_stat(path, *args, **kwargs):
            if str(path) == "/var/run/reboot-required":
                return None
            raise FileNotFoundError(2, "no such file", str(path))

        with patch.object(sys, "platform", "linux"), \
                patch("os.stat", side_effect=fake_stat), \
                patch("subprocess.run", side_effect=AssertionError("dnf must not run when the marker is present")):
            self.assertTrue(collector.reboot_required())

    def test_reboot_required_true_when_dnf_says_restarting(self):
        self._linux_only()
        with patch.object(sys, "platform", "linux"), \
                patch("os.stat",
                      side_effect=FileNotFoundError(2, "no marker")), \
                patch("subprocess.run") as run:
            run.return_value.returncode = 1  # dnf: restart needed
            self.assertTrue(collector.reboot_required())
            self.assertEqual(run.call_args[0][0],
                             ["dnf", "needs-restarting", "-r"])

    def test_reboot_required_false_on_linux_without_marker(self):
        self._linux_only()
        with patch.object(sys, "platform", "linux"), \
                patch("os.stat",
                      side_effect=FileNotFoundError(2, "no marker")), \
                patch("subprocess.run",
                      side_effect=FileNotFoundError("dnf not installed")):
            self.assertFalse(collector.reboot_required())

    def test_windows_probe_reads_both_registry_keys(self):
        calls = []

        class _FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(key, path):
            calls.append(path)
            if "RebootPending" in path:
                raise OSError("key absent")
            return _FakeKey()

        fake_mod = type(sys)("winreg")
        fake_mod.OpenKey = staticmethod(fake_open)
        fake_mod.HKEY_LOCAL_MACHINE = 0
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": fake_mod}):
            self.assertTrue(collector.reboot_required())
        self.assertEqual(
            calls[0],
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending")
        self.assertEqual(
            calls[1],
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired")

    def test_windows_probe_false_when_no_key_present(self):
        def fake_open(key, path):
            raise OSError("key absent")

        fake_mod = type(sys)("winreg")
        fake_mod.OpenKey = staticmethod(fake_open)
        fake_mod.HKEY_LOCAL_MACHINE = 0
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": fake_mod}):
            self.assertFalse(collector.reboot_required())

    def test_checkin_payload_carries_reboot_required(self):
        sent = {}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.update(json)
            return _Resp()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(client.requests, "post", fake_post):
            client.checkin(_config(tmp), metrics=[], reboot_required=True)
        self.assertTrue(sent["reboot_required"])
        self.assertEqual(sent["metrics"], [])

    def test_checkin_without_probe_omits_key(self):
        # The server treats an absent key as "agent too old to report it"
        # and leaves the stored value alone, so the key must simply be
        # absent rather than defaulting to False.
        sent = {}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.update(json)
            return _Resp()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(client.requests, "post", fake_post):
            client.checkin(_config(tmp), metrics=[])
        self.assertNotIn("reboot_required", sent)


if __name__ == "__main__":
    unittest.main()


class DeferralStateFailureTests(unittest.TestCase):
    """A reboot must never fail because its state file could not be written."""

    def test_unwritable_state_dir_does_not_block_the_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            readonly = Path(tmp) / "ro"
            readonly.mkdir()
            os.chmod(readonly, 0o500)
            try:
                config = AgentConfig(server_url="https://x", agent_token="t")
                config.data_dir = readonly
                with patch.object(executor, "_run") as run, \
                     patch.object(executor.sys, "platform", "win32"):
                    run.return_value = "ok"
                    executor._reboot(
                        {"delay_seconds": 0, "defer_limit": 3, "task_id": "t1"}, config
                    )
                argv = run.call_args[0][0]
                self.assertEqual(argv[:4], ["shutdown", "/r", "/t", "0"])
            finally:
                os.chmod(readonly, 0o700)
