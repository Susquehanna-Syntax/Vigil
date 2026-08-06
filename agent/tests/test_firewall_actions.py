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
