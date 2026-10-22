"""Power and sleep actions on the agent.

Nothing here may touch a real machine: every ``powercfg``/``systemctl``/
``pmset`` call is mocked, and the logind drop-in is pointed at a temp dir.
The argv lists are pinned exactly — they are the contract with three vendor
CLIs, and a silently reordered argument is a step that "succeeds" while
changing nothing.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.actions import power
from vigil_agent.config import AgentConfig
from vigil_agent.executor import _HANDLERS


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control"}
    base.update(kw)
    return AgentConfig(**base)


class _Runs:
    """Records every ex._run argv and hands back canned output."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        return "ok"


def _platform(name):
    from types import SimpleNamespace

    return SimpleNamespace(platform=name)


class _Base(unittest.TestCase):
    """Patches the four names power.py reads: the command seam, sys.platform,
    shutil.which and LOGIND_DROPIN. Nothing here may reach a real CLI."""

    platform = "linux"

    def setUp(self):
        self.runs = _Runs()
        self.tmp = Path(tempfile.mkdtemp())
        self.dropin = self.tmp / "logind.conf.d" / "60-vigil-power.conf"
        for patcher in (
            # The seam forwards to executor._run, so patching it there is what
            # production actually calls.
            patch.object(executor, "_run", self.runs),
            patch.object(power, "sys", _platform(self.platform)),
            patch.object(shutil, "which", return_value="/usr/bin/powerprofilesctl"),
            patch.object(power, "LOGIND_DROPIN", self.dropin),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dropin_text(self):
        return self.dropin.read_text()


class WindowsSettingsTests(_Base):
    platform = "win32"

    def test_sleep_timeout_pins_both_powercfg_argv(self):
        out = power._set_sleep_timeout({"ac_minutes": 30, "dc_minutes": 10}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/change", "standby-timeout-ac", "30"],
            ["powercfg", "/change", "standby-timeout-dc", "10"],
        ])
        self.assertEqual(out.data, {"ac_minutes": 30, "dc_minutes": 10})

    def test_dc_defaults_to_the_ac_value(self):
        power._set_sleep_timeout({"ac_minutes": "20"}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/change", "standby-timeout-ac", "20"],
            ["powercfg", "/change", "standby-timeout-dc", "20"],
        ])

    def test_display_timeout_pins_both_powercfg_argv(self):
        power._set_display_timeout({"ac_minutes": 10, "dc_minutes": 0}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/change", "monitor-timeout-ac", "10"],
            ["powercfg", "/change", "monitor-timeout-dc", "0"],
        ])

    def test_hibernate_on_and_off(self):
        power._set_hibernate({"enabled": "yes"}, _config())
        power._set_hibernate({"enabled": False}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/hibernate", "on"],
            ["powercfg", "/hibernate", "off"],
        ])

    def test_plan_aliases_are_the_powercfg_scheme_names(self):
        power._set_power_plan({"plan": "high_performance"}, _config())
        power._set_power_plan({"plan": "power_saver"}, _config())
        power._set_power_plan({"plan": "balanced"}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/setactive", "SCHEME_MIN"],
            ["powercfg", "/setactive", "SCHEME_MAX"],
            ["powercfg", "/setactive", "SCHEME_BALANCED"],
        ])

    def test_lid_sets_ac_dc_then_activates_the_scheme(self):
        out = power._set_lid_action({"ac": "nothing", "dc": "sleep"}, _config())
        self.assertEqual(self.runs.calls, [
            ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS",
             "LIDACTION", "0"],
            ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS",
             "LIDACTION", "1"],
            ["powercfg", "/setactive", "SCHEME_CURRENT"],
        ])
        self.assertEqual(out.data, {"ac": "nothing", "dc": "sleep"})

    def test_dc_lid_defaults_to_ac(self):
        power._set_lid_action({"ac": "shutdown"}, _config())
        self.assertEqual(self.runs.calls[0][-1], "3")
        self.assertEqual(self.runs.calls[1][-1], "3")


class LinuxTests(_Base):
    platform = "linux"

    def test_sleep_timeout_writes_the_dropin_and_hups_logind(self):
        power._set_sleep_timeout({"ac_minutes": 30}, _config())
        text = self.dropin_text()
        self.assertIn("[Login]", text)
        self.assertIn("IdleAction=suspend", text)
        self.assertIn("IdleActionSec=30min", text)
        self.assertEqual(self.runs.calls,
                         [["systemctl", "kill", "-s", "HUP", "systemd-logind"]])

    def test_zero_minutes_means_ignore_and_drops_the_seconds_key(self):
        power._set_sleep_timeout({"ac_minutes": 30}, _config())
        power._set_sleep_timeout({"ac_minutes": 0}, _config())
        text = self.dropin_text()
        self.assertIn("IdleAction=ignore", text)
        self.assertNotIn("IdleActionSec", text)

    def test_a_second_call_keeps_the_idle_keys(self):
        power._set_sleep_timeout({"ac_minutes": 30}, _config())
        power._set_lid_action({"ac": "nothing", "dc": "sleep"}, _config())
        text = self.dropin_text()
        self.assertIn("IdleAction=suspend", text)
        self.assertIn("IdleActionSec=30min", text)
        self.assertIn("HandleLidSwitchExternalPower=ignore", text)
        self.assertIn("HandleLidSwitch=suspend", text)
        self.assertEqual(text.splitlines()[0],
                         "# Managed by Vigil (power task actions). Edits here are overwritten.")

    def test_keys_the_actions_do_not_own_survive_in_another_dropin(self):
        power._set_sleep_timeout({"ac_minutes": 30}, _config())
        other = self.dropin.parent / "10-operator.conf"
        other.write_text("[Login]\nHandlePowerKey=ignore\n")
        power._set_lid_action({"ac": "sleep"}, _config())
        self.assertIn("HandlePowerKey=ignore", other.read_text())
        self.assertNotIn("HandlePowerKey", self.dropin_text())
        text = self.dropin_text()
        self.assertIn("IdleAction=suspend", text)
        self.assertIn("HandleLidSwitch=suspend", text)

    def test_hibernate_off_masks_the_targets(self):
        power._set_hibernate({"enabled": "off"}, _config())
        self.assertEqual(self.runs.calls[0], [
            "systemctl", "mask", "hibernate.target", "hybrid-sleep.target"])

    def test_display_timeout_is_unsupported(self):
        with self.assertRaises(RuntimeError) as ctx:
            power._set_display_timeout({"ac_minutes": 10}, _config())
        self.assertIn("not supported on Linux", str(ctx.exception))

    def test_plan_without_powerprofilesctl_is_unsupported(self):
        with patch.object(shutil, "which", return_value=None), \
                self.assertRaises(RuntimeError) as ctx:
            power._set_power_plan({"plan": "balanced"}, _config())
        self.assertIn("not supported", str(ctx.exception))
        self.assertEqual(self.runs.calls, [])

    def test_plan_argv_with_powerprofilesctl(self):
        power._set_power_plan({"plan": "high_performance"}, _config())
        self.assertEqual(self.runs.calls,
                         [["powerprofilesctl", "set", "performance"]])


class MacTests(_Base):
    platform = "darwin"

    def test_sleep_timeout_uses_pmset_per_power_source(self):
        power._set_sleep_timeout({"ac_minutes": 15, "dc_minutes": 2}, _config())
        self.assertEqual(self.runs.calls, [
            ["pmset", "-c", "sleep", "15"],
            ["pmset", "-b", "sleep", "2"],
        ])

    def test_display_timeout_uses_displaysleep(self):
        power._set_display_timeout({"ac_minutes": 5}, _config())
        self.assertEqual(self.runs.calls, [
            ["pmset", "-c", "displaysleep", "5"],
            ["pmset", "-b", "displaysleep", "5"],
        ])

    def test_hibernate_maps_to_hibernatemode(self):
        power._set_hibernate({"enabled": True}, _config())
        power._set_hibernate({"enabled": False}, _config())
        self.assertEqual(self.runs.calls, [
            ["pmset", "-a", "hibernatemode", "3"],
            ["pmset", "-a", "hibernatemode", "0"],
        ])

    def test_lid_and_hibernate_now_are_unsupported(self):
        with self.assertRaises(RuntimeError) as ctx:
            power._set_lid_action({"ac": "sleep"}, _config())
        self.assertIn("not supported on macOS", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            power._hibernate_now({}, _config())
        self.assertIn("not supported on macOS", str(ctx.exception))


class _Detached(unittest.TestCase):
    """The one seam the detached launch uses, mocked so nothing spawns."""

    def setUp(self):
        self.popens = []
        for patcher in (
            patch.object(power.subprocess, "Popen", side_effect=self._record),
            patch.object(shutil, "which", return_value="/usr/bin/powerprofilesctl"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _record(self, argv, **kwargs):
        self.popens.append((list(argv), kwargs))
        return "proc"


class SleepNowLinuxTests(_Base):
    platform = "linux"

    def test_linux_uses_a_systemd_timer(self):
        out = power._sleep_now({}, _config())
        self.assertEqual(self.runs.calls, [[
            "systemd-run", "--on-active=15", "--unit", "vigil-sleep-now",
            "systemctl", "suspend"]])
        self.assertEqual(out.data, {"delay_seconds": 15})

    def test_hibernate_uses_its_own_unit(self):
        power._hibernate_now({"delay_seconds": 60}, _config())
        self.assertEqual(self.runs.calls, [[
            "systemd-run", "--on-active=60", "--unit", "vigil-hibernate-now",
            "systemctl", "hibernate"]])


class SleepNowWindowsTests(_Detached):
    def setUp(self):
        super().setUp()
        patcher = patch.object(power, "sys", _platform("win32"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sleep_launches_powershell_detached_after_the_delay(self):
        out = power._sleep_now({}, _config())
        argv, kwargs = self.popens[0]
        self.assertEqual(argv[:4], ["powershell.exe", "-NoProfile",
                                    "-NonInteractive", "-Command"])
        self.assertIn("Start-Sleep -Seconds 15", argv[4])
        self.assertIn("SetSuspendState", argv[4])
        self.assertNotEqual(kwargs.get("creationflags"), 0)
        self.assertTrue(kwargs.get("close_fds"))
        self.assertIs(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(out.data, {"delay_seconds": 15})

    def test_hibernate_launches_shutdown_detached(self):
        power._hibernate_now({"delay_seconds": 30}, _config())
        argv, kwargs = self.popens[0]
        self.assertIn("Start-Sleep -Seconds 30", argv[4])
        self.assertIn("shutdown.exe /h", argv[4])
        self.assertNotEqual(kwargs.get("creationflags"), 0)


class SleepNowMacTests(_Detached):
    def setUp(self):
        super().setUp()
        patcher = patch.object(power, "sys", _platform("darwin"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sleep_uses_a_detached_shell(self):
        power._sleep_now({"delay_seconds": 20}, _config())
        argv, kwargs = self.popens[0]
        self.assertEqual(argv, ["sh", "-c", "sleep 20; pmset sleepnow"])
        self.assertTrue(kwargs.get("start_new_session"))

    def test_hibernate_is_unsupported(self):
        with self.assertRaises(RuntimeError) as ctx:
            power._hibernate_now({}, _config())
        self.assertIn("not supported on macOS", str(ctx.exception))
        self.assertEqual(self.popens, [])


class ValidationTests(_Base):
    platform = "win32"

    def test_minutes_out_of_range_or_unparseable_are_refused(self):
        for bad in (-1, 1441, "abc", True, None):
            with self.subTest(ac_minutes=bad), self.assertRaises(ValueError):
                power._set_sleep_timeout({"ac_minutes": bad}, _config())

    def test_zero_and_numeric_strings_are_accepted(self):
        power._set_sleep_timeout({"ac_minutes": "0"}, _config())
        power._set_sleep_timeout({"ac_minutes": 1440}, _config())

    def test_unknown_plan_is_refused_with_the_allowed_values(self):
        with self.assertRaises(ValueError) as ctx:
            power._set_power_plan({"plan": "turbo"}, _config())
        self.assertIn("balanced", str(ctx.exception))
        self.assertIn("power_saver", str(ctx.exception))

    def test_delay_seconds_below_five_is_refused(self):
        with self.assertRaises(ValueError):
            power._sleep_now({"delay_seconds": 2}, _config())
        self.assertEqual(self.runs.calls, [])

    def test_enabled_must_be_a_boolean_word(self):
        with self.assertRaises(ValueError):
            power._set_hibernate({"enabled": "maybe"}, _config())

    def test_nothing_runs_when_validation_fails(self):
        with self.assertRaises(ValueError):
            power._set_display_timeout({"ac_minutes": 5000}, _config())
        self.assertEqual(self.runs.calls, [])


class WiringTests(unittest.TestCase):
    def test_the_executor_routes_all_seven(self):
        for action in ("set_sleep_timeout", "set_display_timeout", "set_hibernate",
                       "set_power_plan", "set_lid_action", "sleep_now",
                       "hibernate_now"):
            self.assertIn(action, _HANDLERS, action)

    def test_a_managed_agent_runs_what_its_allowlist_names_and_nothing_else(self):
        # Like reboot: allowlistable, and off until the allowlist says so.
        everything = {"set_sleep_timeout", "set_display_timeout", "set_hibernate",
                      "set_power_plan", "set_lid_action", "sleep_now", "hibernate_now"}
        managed = AgentConfig(server_url="https://vigil.example.com",
                              agent_token="t" * 40, mode="managed",
                              allowlist=set(everything))
        for action in everything:
            self.assertTrue(managed.task_allowed(action), action)
        settings_only = AgentConfig(server_url="https://vigil.example.com",
                                    agent_token="t" * 40, mode="managed",
                                    allowlist={"set_sleep_timeout"})
        for action in ("sleep_now", "hibernate_now", "set_lid_action"):
            self.assertFalse(settings_only.task_allowed(action), action)

    def test_full_control_allows_sleep_now(self):
        full = AgentConfig(server_url="https://vigil.example.com",
                           agent_token="t" * 40, mode="full_control")
        self.assertTrue(full.task_allowed("sleep_now"))
        self.assertTrue(full.task_allowed("hibernate_now"))


if __name__ == "__main__":
    unittest.main()
