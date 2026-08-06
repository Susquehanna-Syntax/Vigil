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

# Covers three shapes _RULE must not silently drop or mis-parse:
#   - an interface-bound rule ("22/tcp on eth0 ...")
#   - a bare port with no protocol ("25 ...")
#   - an app-profile rule with no numeric port at all ("OpenSSH ...")
UFW_MIXED = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp on eth0             ALLOW IN    Anywhere
25                         ALLOW IN    Anywhere
OpenSSH                    ALLOW IN    Anywhere
"""


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


class UfwUnparsedRuleTests(unittest.TestCase):
    """Rule shapes _RULE must not drop silently: interface-bound rules,
    bare ports with no protocol, and app profiles it can't parse at all."""

    def _snapshot(self, output):
        b = firewall.UfwBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: output):
            return b.snapshot()

    def test_interface_bound_rule_is_parsed(self):
        rules = self._snapshot(UFW_MIXED)["rules"]
        self.assertIn({"port": 22, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": "eth0"}, rules)

    def test_bare_port_has_protocol_any(self):
        rules = self._snapshot(UFW_MIXED)["rules"]
        self.assertIn({"port": 25, "protocol": "any", "action": "allow",
                       "source": "any", "interface": ""}, rules)

    def test_app_profile_rule_lands_in_unparsed_not_dropped(self):
        unparsed = self._snapshot(UFW_MIXED)["unparsed"]
        self.assertTrue(any("OpenSSH" in line for line in unparsed))

    def test_known_good_fixture_has_no_unparsed_lines(self):
        self.assertEqual(self._snapshot(UFW_ACTIVE)["unparsed"], [])

    def test_header_lines_never_land_in_unparsed(self):
        unparsed = self._snapshot(UFW_MIXED)["unparsed"]
        headers = ("Status:", "Logging:", "Default:", "New profiles:", "To", "--")
        for line in unparsed:
            self.assertFalse(line.startswith(headers), line)


FIREWALLD_ALL = """public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  sources:
  services: ssh dhcpv6-client
  ports: 80/tcp 443/tcp 5353/udp
  protocols:
  forward: yes
  masquerade: no
"""


class FirewallCmdParseTests(unittest.TestCase):
    def _snapshot(self, state="running", listing=FIREWALLD_ALL):
        b = firewall.FirewallCmdBackend()

        def fake_run(cmd, timeout=None):
            return state if "--state" in cmd else listing

        with patch.object(firewall, "_run", fake_run):
            return b.snapshot()

    def test_tool_is_named(self):
        self.assertEqual(self._snapshot()["tool"], "firewall-cmd")

    def test_enabled_is_read(self):
        self.assertTrue(self._snapshot()["enabled"])
        self.assertFalse(self._snapshot(state="not running")["enabled"])

    def test_ports_are_parsed(self):
        rules = self._snapshot()["rules"]
        self.assertIn({"port": 443, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": ""}, rules)

    def test_udp_ports_are_parsed(self):
        rules = self._snapshot()["rules"]
        self.assertIn({"port": 5353, "protocol": "udp", "action": "allow",
                       "source": "any", "interface": ""}, rules)

    def test_an_open_port_is_always_an_allow(self):
        """firewalld lists what is permitted; there is no deny list to read."""
        self.assertTrue(all(r["action"] == "allow"
                            for r in self._snapshot()["rules"]))

    def test_no_ports_line_is_not_an_error(self):
        snap = self._snapshot(listing="public (active)\n  target: default\n")
        self.assertEqual(snap["rules"], [])


class FirewallCmdUnparsedRuleTests(unittest.TestCase):
    """Port ranges (e.g. 8000-8100/tcp) can't be expressed as a single
    port/protocol rule; they must be surfaced, not silently dropped."""

    def _snapshot(self, listing):
        b = firewall.FirewallCmdBackend()

        def fake_run(cmd, timeout=None):
            return "running" if "--state" in cmd else listing

        with patch.object(firewall, "_run", fake_run):
            return b.snapshot()

    def test_port_range_lands_in_unparsed_not_dropped(self):
        listing = ("public (active)\n  target: default\n"
                   "  ports: 80/tcp 8000-8100/tcp\n")
        snap = self._snapshot(listing)
        self.assertIn("8000-8100/tcp", snap["unparsed"])
        self.assertIn({"port": 80, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": ""}, snap["rules"])

    def test_known_good_fixture_has_no_unparsed_lines(self):
        self.assertEqual(self._snapshot(FIREWALLD_ALL)["unparsed"], [])
