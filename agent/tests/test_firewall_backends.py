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
                       "source": "any", "interface": "", "name": "",
                       "rule_id": ""}, rules)

    def test_deny_and_source_are_parsed(self):
        rules = self._snapshot(UFW_ACTIVE)["rules"]
        self.assertIn({"port": 8080, "protocol": "tcp", "action": "deny",
                       "source": "10.0.0.0/8", "interface": "", "name": "",
                       "rule_id": ""}, rules)

    def test_rules_carry_an_empty_name_and_rule_id_for_contract_uniformity(self):
        # ufw has no per-rule identity to read back, but every backend must
        # emit the same keys so the server has a single format to parse.
        rules = self._snapshot(UFW_ACTIVE)["rules"]
        self.assertTrue(rules)
        for r in rules:
            self.assertEqual(r["name"], "")
            self.assertEqual(r["rule_id"], "")

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
                       "source": "any", "interface": "eth0", "name": "",
                       "rule_id": ""}, rules)

    def test_bare_port_has_protocol_any(self):
        rules = self._snapshot(UFW_MIXED)["rules"]
        self.assertIn({"port": 25, "protocol": "any", "action": "allow",
                       "source": "any", "interface": "", "name": "",
                       "rule_id": ""}, rules)

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
                       "source": "any", "interface": "", "name": "",
                       "rule_id": ""}, rules)

    def test_udp_ports_are_parsed(self):
        rules = self._snapshot()["rules"]
        self.assertIn({"port": 5353, "protocol": "udp", "action": "allow",
                       "source": "any", "interface": "", "name": "",
                       "rule_id": ""}, rules)

    def test_rules_carry_an_empty_name_and_rule_id_for_contract_uniformity(self):
        # firewalld has no per-rule identity to read back either, but every
        # backend must emit the same keys so the server has a single format
        # to parse.
        rules = self._snapshot()["rules"]
        self.assertTrue(rules)
        for r in rules:
            self.assertEqual(r["name"], "")
            self.assertEqual(r["rule_id"], "")

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
                       "source": "any", "interface": "", "name": "",
                       "rule_id": ""}, snap["rules"])

    def test_known_good_fixture_has_no_unparsed_lines(self):
        self.assertEqual(self._snapshot(FIREWALLD_ALL)["unparsed"], [])


import json as _json

WIN_PROFILES = _json.dumps([
    {"Name": "Domain", "Enabled": True},
    {"Name": "Private", "Enabled": True},
    {"Name": "Public", "Enabled": False},
])

WIN_RULES = _json.dumps([
    {"port": "22", "protocol": "TCP", "action": "Allow", "name": "OpenSSH",
     "rule_id": "{11111111-1111-1111-1111-111111111111}"},
    {"port": "3389", "protocol": "TCP", "action": "Allow", "name": "RDP",
     "rule_id": "{22222222-2222-2222-2222-222222222222}"},
    {"port": "445", "protocol": "TCP", "action": "Block", "name": "Vigil tcp/445",
     "rule_id": "{33333333-3333-3333-3333-333333333333}"},
])


class WindowsParseTests(unittest.TestCase):
    def _snapshot(self, profiles=WIN_PROFILES, rules=WIN_RULES):
        b = firewall.WindowsBackend()

        def fake_run(cmd, timeout=None):
            return profiles if "NetFirewallProfile" in " ".join(cmd) else rules

        with patch.object(firewall, "_run", fake_run):
            return b.snapshot()

    def test_tool_is_named(self):
        self.assertEqual(self._snapshot()["tool"], "windows")

    def test_enabled_when_any_profile_is_on(self):
        self.assertTrue(self._snapshot()["enabled"])

    def test_disabled_when_every_profile_is_off(self):
        off = _json.dumps([{"Name": "Public", "Enabled": False}])
        self.assertFalse(self._snapshot(profiles=off)["enabled"])

    def test_profiles_are_reported(self):
        """Windows profiles can disagree; the tab has to be able to show it."""
        profiles = self._snapshot()["profiles"]
        self.assertIn({"name": "Public", "enabled": False}, profiles)

    def test_rules_are_normalised(self):
        rules = self._snapshot()["rules"]
        self.assertIn({"port": 22, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": "", "name": "OpenSSH",
                       "rule_id": "{11111111-1111-1111-1111-111111111111}"},
                      rules)

    def test_block_becomes_deny(self):
        """PowerShell says Block; the rest of Vigil says deny."""
        rules = self._snapshot()["rules"]
        self.assertIn({"port": 445, "protocol": "tcp", "action": "deny",
                       "source": "any", "interface": "",
                       "name": "Vigil tcp/445",
                       "rule_id": "{33333333-3333-3333-3333-333333333333}"},
                      rules)

    def test_rules_carry_the_displayname(self):
        # The DisplayName is read but previously discarded; it must be
        # threaded through so the UI can show it.
        rules = self._snapshot()["rules"]
        names = {r["name"] for r in rules}
        self.assertEqual(names, {"OpenSSH", "RDP", "Vigil tcp/445"})

    def test_rules_carry_a_distinct_unique_rule_id(self):
        # Critical (task-12 follow-up review): DisplayName is NOT
        # guaranteed unique on Windows -- WindowsBackend.add_rule's own
        # naming scheme could (before this fix) produce two rules sharing
        # one. rule_id is Get-NetFirewallRule's Name (InstanceID), which IS
        # unique, and it must be threaded through distinctly from `name` so
        # remove_rule has something safe to key on.
        rules = self._snapshot()["rules"]
        ids = {r["rule_id"] for r in rules}
        self.assertEqual(ids, {
            "{11111111-1111-1111-1111-111111111111}",
            "{22222222-2222-2222-2222-222222222222}",
            "{33333333-3333-3333-3333-333333333333}",
        })
        for r in rules:
            self.assertNotEqual(r["name"], r["rule_id"])

    def test_two_rules_sharing_a_displayname_keep_distinct_rule_ids(self):
        # The exact collision the review flagged: an allow and a scoped
        # deny on the same port, both named "Vigil tcp/8443" by the OLD
        # add_rule naming scheme (pre-fix). Even with identical `name`
        # values, `rule_id` must still distinguish them.
        rules = _json.dumps([
            {"port": "8443", "protocol": "TCP", "action": "Allow",
             "name": "Vigil tcp/8443", "rule_id": "{aaaa}"},
            {"port": "8443", "protocol": "TCP", "action": "Block",
             "name": "Vigil tcp/8443", "rule_id": "{bbbb}"},
        ])
        snap = self._snapshot(rules=rules)
        by_id = {r["rule_id"]: r for r in snap["rules"]}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["{aaaa}"]["action"], "allow")
        self.assertEqual(by_id["{bbbb}"]["action"], "deny")
        self.assertEqual(by_id["{aaaa}"]["name"], "Vigil tcp/8443")
        self.assertEqual(by_id["{bbbb}"]["name"], "Vigil tcp/8443")

    def test_a_single_object_is_handled(self):
        """ConvertTo-Json emits a bare object, not a list, for one result."""
        one = _json.dumps({"port": "22", "protocol": "TCP", "action": "Allow",
                           "name": "OpenSSH"})
        self.assertEqual(len(self._snapshot(rules=one)["rules"]), 1)

    def test_unparseable_output_yields_no_rules(self):
        self.assertEqual(self._snapshot(rules="not json")["rules"], [])


class WindowsUnparsedRuleTests(unittest.TestCase):
    """Port shapes Vigil's single port/protocol rule model can't express,
    and whole-payload read failures, must land in `unparsed` rather than
    vanishing -- an empty rule list should never be confused with a read
    that failed."""

    def _snapshot(self, profiles=WIN_PROFILES, rules=WIN_RULES):
        b = firewall.WindowsBackend()

        def fake_run(cmd, timeout=None):
            return profiles if "NetFirewallProfile" in " ".join(cmd) else rules

        with patch.object(firewall, "_run", fake_run):
            return b.snapshot()

    def test_port_range_lands_in_unparsed_not_dropped(self):
        rules = _json.dumps([
            {"port": "8000-8100", "protocol": "TCP", "action": "Allow",
             "name": "Range Rule"},
        ])
        unparsed = self._snapshot(rules=rules)["unparsed"]
        self.assertTrue(any("Range Rule" in line and "8000-8100" in line
                            for line in unparsed))

    def test_port_list_lands_in_unparsed_not_dropped(self):
        rules = _json.dumps([
            {"port": "80,443", "protocol": "TCP", "action": "Allow",
             "name": "List Rule"},
        ])
        unparsed = self._snapshot(rules=rules)["unparsed"]
        self.assertTrue(any("List Rule" in line and "80,443" in line
                            for line in unparsed))

    def test_unparseable_rule_json_yields_nonempty_unparsed(self):
        """A snapshot that failed to read rules has to be visibly different
        from a host that legitimately has none."""
        snap = self._snapshot(rules="not json")
        self.assertEqual(snap["rules"], [])
        self.assertTrue(snap["unparsed"])

    def test_unparseable_profile_json_yields_nonempty_unparsed(self):
        snap = self._snapshot(profiles="not json")
        self.assertEqual(snap["profiles"], [])
        self.assertTrue(snap["unparsed"])


class WindowsUnknownStateTests(unittest.TestCase):
    """A snapshot must never round an unknown state to a known one: a
    failed profile read has to leave `enabled` as None (unknown), not False
    (a confident "disabled"); a scalar PowerShell payload must not crash the
    read; and an unrecognised rule action must not be guessed as "allow",
    since that direction understates the host's exposure."""

    def _snapshot(self, profiles=WIN_PROFILES, rules=WIN_RULES):
        b = firewall.WindowsBackend()

        def fake_run(cmd, timeout=None):
            return profiles if "NetFirewallProfile" in " ".join(cmd) else rules

        with patch.object(firewall, "_run", fake_run):
            return b.snapshot()

    def test_scalar_rules_payload_does_not_raise(self):
        snap = self._snapshot(rules="5")
        self.assertEqual(snap["rules"], [])
        self.assertTrue(snap["unparsed"])

    def test_scalar_profiles_payload_does_not_raise(self):
        snap = self._snapshot(profiles="5")
        self.assertEqual(snap["profiles"], [])

    def test_enabled_is_none_when_profile_read_fails(self):
        snap = self._snapshot(profiles="not json")
        self.assertIsNone(snap["enabled"])

    def test_enabled_is_false_when_every_profile_is_genuinely_off(self):
        """Distinct from the failed-read case: a successful read of all-off
        profiles is a known False, not an unknown None."""
        off = _json.dumps([{"Name": "Public", "Enabled": False}])
        snap = self._snapshot(profiles=off)
        self.assertIs(snap["enabled"], False)

    def test_unrecognised_action_produces_no_rule_but_is_recorded(self):
        rules = _json.dumps([
            {"port": "8443", "protocol": "TCP", "action": "NotConfigured",
             "name": "Odd Rule"},
        ])
        snap = self._snapshot(rules=rules)
        self.assertFalse(any(r["port"] == 8443 for r in snap["rules"]))
        self.assertTrue(any("Odd Rule" in line and "NotConfigured" in line
                            for line in snap["unparsed"]))

    def test_allow_action_still_maps_to_allow(self):
        rules = _json.dumps([
            {"port": "8443", "protocol": "TCP", "action": "Allow",
             "name": "Fine Rule", "rule_id": "{cccc}"},
        ])
        snap = self._snapshot(rules=rules)
        self.assertIn({"port": 8443, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": "", "name": "Fine Rule",
                       "rule_id": "{cccc}"},
                      snap["rules"])
