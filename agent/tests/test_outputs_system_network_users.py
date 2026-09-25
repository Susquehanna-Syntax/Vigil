"""System, firewall, user and cron actions report the facts a later step branches on."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t", mode="full_control",
                data_dir=Path(tempfile.gettempdir()))
    base.update(kw)
    return AgentConfig(**base)


def _ok_run(cmd, timeout=None, extra_env=None):
    return "ok"


class FakeFirewall:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def add_rule(self, *a, **k): return "added"
    def remove_rule(self, *a, **k): return "removed"
    def set_policy(self, *a, **k): return "policy set"
    def set_enabled(self, on): return "on" if on else "off"
    def snapshot(self): return dict(self._snapshot)


SNAP = {"tool": "ufw", "enabled": True, "defaults": {"incoming": "deny", "outgoing": "allow"},
        "rules": [{"port": "22"}, {"port": "80"}], "unparsed": []}


class SystemOutputTests(unittest.TestCase):
    def test_hostname_reports_name(self):
        with patch.object(executor, "_run", _ok_run):
            self.assertEqual(executor._set_hostname({"hostname": "web-01"}, _config()).data,
                             {"hostname": "web-01"})

    def test_reboot_reports_delay(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "platform", "linux"), \
                patch.object(executor, "_run", _ok_run):
            out = executor._reboot({"delay_seconds": 120}, _config(data_dir=Path(tmp)))
        self.assertEqual(out.data, {"delay_seconds": 120, "deferral_active": False})

    def test_temp_files_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(executor.tempfile, "gettempdir", return_value=tmp):
            out = executor._clear_temp_files({"older_than_days": 0}, _config())
        self.assertEqual(set(out.data), {"removed", "skipped"})
        self.assertTrue(all(isinstance(v, int) for v in out.data.values()))


class FirewallOutputTests(unittest.TestCase):
    def _with(self, snapshot=SNAP):
        return patch.object(executor.firewall, "detect", return_value=FakeFirewall(snapshot))

    def test_firewall_rule_actions_report_rule(self):
        with self._with():
            for handler in (executor._add_firewall_rule, executor._remove_firewall_rule):
                out = handler({"port": 443, "protocol": "tcp", "action": "allow"}, _config())
                self.assertEqual(out.data, {"port": "443", "protocol": "tcp", "action": "allow"})

    def test_list_firewall_rules_text_unchanged_and_counts(self):
        with self._with():
            out = executor._list_firewall_rules({}, _config())
        self.assertEqual(str(out), json.dumps({**SNAP, "supported": True}))
        self.assertEqual(out.data, {"supported": True, "enabled": True, "rule_count": 2})
        with patch.object(executor.firewall, "detect", return_value=None):
            out = executor._list_firewall_rules({}, _config())
        self.assertEqual(out.data, {"supported": False, "enabled": False, "rule_count": 0})

    def test_firewall_policy_and_toggle(self):
        with self._with():
            self.assertEqual(executor._set_firewall_policy(
                {"direction": "incoming", "policy": "deny"}, _config()).data,
                {"direction": "incoming", "policy": "deny"})
            self.assertEqual(executor._enable_firewall({}, _config()).data, {"enabled": True})
            self.assertEqual(executor._disable_firewall({}, _config()).data, {"enabled": False})


class UserAndCronOutputTests(unittest.TestCase):
    def test_user_actions_report_username(self):
        with patch.object(executor, "_run", _ok_run):
            self.assertEqual(executor._create_user({"username": "deploy"}, _config()).data, {"username": "deploy"})
            self.assertEqual(executor._delete_user({"username": "deploy"}, _config()).data, {"username": "deploy"})
            self.assertEqual(executor._add_user_to_group({"username": "deploy", "group": "docker"}, _config()).data,
                             {"username": "deploy", "group": "docker"})

    def test_cron_actions_report_counts(self):
        crontab = "0 1 * * * /opt/backup.sh\n5 1 * * * /opt/other.sh\n"
        def fake(cmd, **kw):
            if "-l" in cmd:
                return SimpleNamespace(returncode=0, stdout=crontab, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(executor.subprocess, "run", side_effect=fake):
            out = executor._delete_cron_job({"user": "root", "pattern": "backup"}, _config())
            self.assertEqual(out.data, {"user": "root", "removed": 1})
            out = executor._delete_cron_job({"user": "root", "pattern": "nomatch"}, _config())
            self.assertEqual(out.data, {"user": "root", "removed": 0})
            out = executor._create_cron_job({"user": "root", "schedule": "0 2 * * *",
                                             "command": "/opt/x.sh"}, _config())
            self.assertEqual(out.data, {"user": "root"})


if __name__ == "__main__":
    unittest.main()
