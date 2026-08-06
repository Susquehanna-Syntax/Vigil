"""Firewall backends parse real tool output into one shape.

The server never screen-scrapes `ufw status`; the agent hands over a parsed
snapshot, so all three backends have to converge on the same contract.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import firewall

UFW_ACTIVE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
80/tcp                     ALLOW IN    Anywhere
8080/tcp                   DENY IN     10.0.0.0/8
22/tcp (v6)                ALLOW IN    Anywhere (v6)
"""

UFW_INACTIVE = "Status: inactive\n"


class UfwParseTests(unittest.TestCase):
    def _snapshot(self, output):
        b = firewall.UfwBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: output):
            return b.snapshot()

    def test_tool_is_named(self):
        self.assertEqual(self._snapshot(UFW_ACTIVE)["tool"], "ufw")

    def test_enabled_is_read(self):
        self.assertTrue(self._snapshot(UFW_ACTIVE)["enabled"])
        self.assertFalse(self._snapshot(UFW_INACTIVE)["enabled"])

    def test_default_policies_are_read(self):
        d = self._snapshot(UFW_ACTIVE)["defaults"]
        self.assertEqual(d["incoming"], "deny")
        self.assertEqual(d["outgoing"], "allow")

    def test_rules_are_parsed(self):
        rules = self._snapshot(UFW_ACTIVE)["rules"]
        self.assertIn({"port": 22, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": ""}, rules)

    def test_deny_and_source_are_parsed(self):
        rules = self._snapshot(UFW_ACTIVE)["rules"]
        self.assertIn({"port": 8080, "protocol": "tcp", "action": "deny",
                       "source": "10.0.0.0/8", "interface": ""}, rules)

    def test_v6_duplicates_are_collapsed(self):
        """ufw lists each rule twice on dual-stack hosts. Showing 22/tcp
        twice in the tab would be noise, and removing one would look like it
        failed."""
        rules = self._snapshot(UFW_ACTIVE)["rules"]
        self.assertEqual(sum(1 for r in rules if r["port"] == 22), 1)

    def test_an_inactive_firewall_has_no_rules(self):
        self.assertEqual(self._snapshot(UFW_INACTIVE)["rules"], [])
