"""The firewall actions. Reads must never raise on a host that simply has no
firewall tool: "this host has no supported firewall" is an answer, and turning
it into a task failure buries it in an error nobody reads."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, firewall
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t",
                mode="full_control")
    base.update(kw)
    return AgentConfig(**base)


SNAPSHOT = {"tool": "ufw", "enabled": True,
            "defaults": {"incoming": "deny", "outgoing": "allow"},
            "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": ""}]}


class FakeBackend:
    name = "ufw"

    def available(self):
        return True

    def snapshot(self):
        return SNAPSHOT


class ListRulesTests(unittest.TestCase):
    def test_the_snapshot_is_returned_as_json(self):
        with patch.object(firewall, "detect", lambda: FakeBackend()):
            out = executor._list_firewall_rules({}, _config())
        self.assertEqual(json.loads(out), SNAPSHOT)

    def test_no_supported_tool_is_an_answer_not_a_failure(self):
        with patch.object(firewall, "detect", lambda: None):
            out = executor._list_firewall_rules({}, _config())
        data = json.loads(out)
        self.assertFalse(data["supported"])
        self.assertEqual(data["rules"], [])

    def test_no_supported_tool_still_carries_unparsed(self):
        with patch.object(firewall, "detect", lambda: None):
            out = executor._list_firewall_rules({}, _config())
        data = json.loads(out)
        self.assertEqual(data["unparsed"], [])

    def test_it_is_registered_as_an_action(self):
        # The dispatch table is named `_HANDLERS` in executor.py (see
        # test_step_timeout.py for the existing convention); the brief
        # refers to it descriptively as "ACTION_HANDLERS" but no such
        # symbol exists.
        self.assertIn("list_firewall_rules", executor._HANDLERS)

    def test_it_is_allowlistable(self):
        from vigil_agent.config import _ALL_ACTIONS
        self.assertIn("list_firewall_rules", _ALL_ACTIONS)


class RemoveDenyRuleTests(unittest.TestCase):
    """The bug: the ufw branch ran `ufw delete allow <port>` with `allow`
    hardcoded, so a rule added with action=deny could not be removed by the
    action whose only job is removing it."""

    def _cmd(self, params):
        seen = {}

        def fake_run(cmd, timeout=None):
            if cmd[:1] == ["which"]:
                return "/usr/sbin/ufw"
            seen["cmd"] = cmd
            return "ok"

        with patch.object(firewall, "_run", fake_run), \
             patch.object(firewall.shutil, "which", lambda t: "/usr/sbin/ufw"
                          if t == "ufw" else None):
            executor._remove_firewall_rule(params, _config())
        return seen["cmd"]

    def test_a_deny_rule_is_removed_as_deny(self):
        cmd = self._cmd({"port": 8080, "protocol": "tcp", "action": "deny"})
        self.assertIn("deny", cmd)
        self.assertNotIn("allow", cmd)

    def test_allow_remains_the_default(self):
        cmd = self._cmd({"port": 80, "protocol": "tcp"})
        self.assertIn("allow", cmd)


class FirewallCmdReloadTests(unittest.TestCase):
    """The bug: --permanent changes were written and never reloaded, so the
    task reported success while the running firewall was unchanged."""

    def _cmds(self, fn, params):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        with patch.object(firewall, "_run", fake_run), \
             patch.object(firewall.shutil, "which",
                          lambda t: "/usr/bin/firewall-cmd"
                          if t == "firewall-cmd" else None):
            fn(params, _config())
        return calls

    def test_adding_reloads(self):
        calls = self._cmds(executor._add_firewall_rule,
                           {"port": 443, "protocol": "tcp"})
        self.assertTrue(any("--reload" in c for c in calls),
                        "a permanent change that is never reloaded does nothing")

    def test_removing_reloads(self):
        calls = self._cmds(executor._remove_firewall_rule,
                           {"port": 443, "protocol": "tcp"})
        self.assertTrue(any("--reload" in c for c in calls))

    def test_a_deny_with_no_source_adds_a_reject_rich_rule(self):
        # Corrected behaviour: firewalld expresses "not allowed" by absence,
        # so an explicit deny must add a reject rich rule -- not fall back to
        # --remove-port, which is a silent no-op when the port was never
        # open and would still be reported as success.
        calls = self._cmds(executor._add_firewall_rule,
                           {"port": 8080, "protocol": "tcp", "action": "deny"})
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("--add-rich-rule" in c and "reject" in c for c in joined))
        self.assertFalse(any("--remove-port" in c for c in joined))


class SourceValidationTests(unittest.TestCase):
    def test_a_valid_cidr_is_accepted(self):
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"), \
             patch.object(firewall.shutil, "which",
                          lambda t: "/usr/sbin/ufw" if t == "ufw" else None):
            executor._add_firewall_rule(
                {"port": 22, "protocol": "tcp", "source": "10.0.0.0/8"},
                _config())

    def test_a_shell_metacharacter_is_rejected(self):
        with self.assertRaises(ValueError):
            executor._add_firewall_rule(
                {"port": 22, "protocol": "tcp", "source": "10.0.0.0/8; rm -rf /"},
                _config())

    def test_a_bogus_source_is_rejected(self):
        with self.assertRaises(ValueError):
            executor._add_firewall_rule(
                {"port": 22, "protocol": "tcp", "source": "not-an-address"},
                _config())

    def test_validate_source_accepts_any(self):
        self.assertEqual(firewall.validate_source("any"), "any")

    def test_validate_source_accepts_a_bare_ipv4(self):
        self.assertEqual(firewall.validate_source("192.168.1.1"),
                         "192.168.1.1")

    def test_validate_source_accepts_a_cidr(self):
        self.assertEqual(firewall.validate_source("10.0.0.0/8"), "10.0.0.0/8")

    def test_validate_source_rejects_a_bare_slash(self):
        with self.assertRaises(ValueError):
            firewall.validate_source("/")


class WindowsWriteTests(unittest.TestCase):
    def test_a_deny_add_produces_action_block(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.add_rule(3389, "tcp", "deny")

        self.assertEqual(len(calls), 1)
        self.assertIn("action=block", calls[0])
        self.assertNotIn("action=allow", calls[0])
