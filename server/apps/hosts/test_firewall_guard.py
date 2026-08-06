"""The tab refuses changes that would cut off access to the host.

The task editor stays unguarded on purpose — that is the escape hatch. This
guard exists so a form cannot do by accident what a hand-written task can do
deliberately.
"""
from django.test import TestCase

from apps.hosts.firewall_guard import check_change

LINUX = {"tool": "ufw", "enabled": True,
         "defaults": {"incoming": "deny", "outgoing": "allow"},
         "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                    "source": "any", "interface": ""}]}


class Host:
    def __init__(self, os="Ubuntu 24.04"):
        self.os = os


def add(port, action="allow", protocol="tcp"):
    return {"action": "add_firewall_rule",
            "params": {"port": port, "protocol": protocol, "action": action}}


def remove(port, protocol="tcp"):
    return {"action": "remove_firewall_rule",
            "params": {"port": port, "protocol": protocol}}


def policy(direction, value):
    return {"action": "set_firewall_policy",
            "params": {"direction": direction, "policy": value}}


class LockoutGuardTests(TestCase):
    def test_an_ordinary_rule_is_allowed(self):
        self.assertEqual(check_change(Host(), LINUX, add(8080)), "")

    def test_denying_ssh_is_refused(self):
        self.assertIn("22", check_change(Host(), LINUX, add(22, "deny")))

    def test_removing_the_ssh_allow_rule_is_refused(self):
        self.assertNotEqual(check_change(Host(), LINUX, remove(22)), "")

    def test_denying_outbound_is_refused(self):
        """The worst one: agents are outbound-only, so this severs the agent's
        own link to Vigil and no task can be dispatched to undo it."""
        msg = check_change(Host(), LINUX, policy("outgoing", "deny"))
        self.assertIn("check in", msg.lower())

    def test_rejecting_outbound_is_refused_too(self):
        self.assertNotEqual(
            check_change(Host(), LINUX, policy("outgoing", "reject")), "")

    def test_allowing_outbound_is_fine(self):
        self.assertEqual(check_change(Host(), LINUX, policy("outgoing", "allow")), "")

    def test_default_deny_inbound_without_an_ssh_rule_is_refused(self):
        bare = {**LINUX, "rules": []}
        self.assertNotEqual(
            check_change(Host(), bare, policy("incoming", "deny")), "")

    def test_default_deny_inbound_with_an_ssh_rule_is_allowed(self):
        self.assertEqual(check_change(Host(), LINUX, policy("incoming", "deny")), "")

    def test_rdp_is_protected_on_windows(self):
        win = {**LINUX, "tool": "windows"}
        msg = check_change(Host(os="Windows Server 2022"), win, add(3389, "deny"))
        self.assertIn("3389", msg)

    def test_rdp_is_not_protected_on_linux(self):
        self.assertEqual(check_change(Host(), LINUX, add(3389, "deny")), "")

    def test_disabling_the_firewall_is_allowed(self):
        """Not a lockout — it opens the host. High risk, but not refused."""
        self.assertEqual(
            check_change(Host(), LINUX,
                         {"action": "disable_firewall", "params": {}}), "")

    def test_a_missing_snapshot_does_not_crash(self):
        self.assertEqual(check_change(Host(), None, add(8080)), "")

    # --- Additional edge cases beyond the brief's list --------------------

    def test_missing_rules_key_does_not_crash_and_refuses_deny_incoming(self):
        """No `rules` at all -- unknown means refuse, not crash."""
        no_rules = {"tool": "ufw", "enabled": True,
                    "defaults": {"incoming": "allow", "outgoing": "allow"}}
        self.assertEqual(check_change(Host(), no_rules, add(8080)), "")
        msg = check_change(Host(), no_rules, policy("incoming", "deny"))
        self.assertNotEqual(msg, "")

    def test_unparsed_ssh_rule_with_no_port_22_entry_refuses_deny_incoming(self):
        """An app-profile rule (e.g. `ufw allow OpenSSH`) lands in
        `unparsed`, not `rules` -- so the guard cannot see that SSH is
        actually allowed. It must refuse rather than assume the host is
        safe: the guard refuses more when it knows less, which is the
        correct, safe direction even though it produces a false refusal
        for a host that is really fine. Do not "fix" this into
        permissiveness -- that would silently trade safety for convenience."""
        app_profile = {"tool": "ufw", "enabled": True,
                       "defaults": {"incoming": "allow", "outgoing": "allow"},
                       "rules": [],
                       "unparsed": ["OpenSSH                    ALLOW IN    Anywhere"]}
        msg = check_change(Host(), app_profile, policy("incoming", "deny"))
        self.assertNotEqual(msg, "")

    def test_unknown_defaults_do_not_crash(self):
        unknown = {**LINUX, "defaults": {"incoming": "unknown", "outgoing": "unknown"}}
        self.assertEqual(check_change(Host(), unknown, add(8080)), "")
        check_change(Host(), unknown, policy("incoming", "deny"))
        check_change(Host(), unknown, policy("outgoing", "deny"))

    def test_enabled_none_does_not_crash(self):
        win = {**LINUX, "tool": "windows", "enabled": None}
        self.assertEqual(
            check_change(Host(os="Windows Server 2022"), win, add(8080)), "")

    def test_missing_params_key_does_not_crash(self):
        change = {"action": "add_firewall_rule"}
        # Must not raise; either an allow or a refusal is acceptable.
        check_change(Host(), LINUX, change)

    def test_non_dict_params_does_not_crash(self):
        change = {"action": "add_firewall_rule", "params": "not-a-dict"}
        # Must not raise; either an allow or a refusal is acceptable.
        check_change(Host(), LINUX, change)

    def test_set_firewall_policy_reject_outgoing_is_refused(self):
        self.assertNotEqual(
            check_change(Host(), LINUX, policy("outgoing", "reject")), "")

    # --- Normalisation / fail-safe-default fixes (review follow-up) -------

    def test_mis_cased_action_name_still_refuses_outbound_deny(self):
        """`action` must be normalised the same way every other dispatch
        field is -- an uppercase variant of a lockout-shaped action must not
        slip past a bare `==` into the permissive fall-through."""
        change = {"action": "Set_Firewall_Policy",
                  "params": {"direction": "outgoing", "policy": "deny"}}
        msg = check_change(Host(), LINUX, change)
        self.assertIn("check in", msg.lower())

    def test_mis_cased_add_firewall_rule_still_refuses_deny_on_port_22(self):
        change = {"action": "Add_Firewall_Rule",
                  "params": {"port": 22, "protocol": "tcp", "action": "DENY"}}
        msg = check_change(Host(), LINUX, change)
        self.assertIn("22", msg)

    def test_direction_and_policy_with_whitespace_still_refuse_outbound(self):
        change = {"action": "set_firewall_policy",
                  "params": {"direction": "outgoing ", "policy": "deny"}}
        msg = check_change(Host(), LINUX, change)
        self.assertIn("check in", msg.lower())

    def test_unknown_action_name_is_refused(self):
        for name in ("reboot", "drop_all_traffic"):
            change = {"action": name, "params": {}}
            msg = check_change(Host(), LINUX, change)
            self.assertNotEqual(msg, "")
            self.assertIn(name, msg)

    def test_enable_and_disable_firewall_are_still_allowed(self):
        """The fail-safe default for unrecognised actions must not have
        swallowed these two legitimate, deliberately-unrefused actions."""
        self.assertEqual(
            check_change(Host(), LINUX,
                         {"action": "enable_firewall", "params": {}}), "")
        self.assertEqual(
            check_change(Host(), LINUX,
                         {"action": "disable_firewall", "params": {}}), "")

    def test_denying_an_unparseable_port_is_refused(self):
        msg = check_change(Host(), LINUX, add("22.0", "deny"))
        self.assertIn("unparseable", msg.lower())

    def test_removing_a_rule_for_a_non_numeric_port_is_refused(self):
        msg = check_change(Host(), LINUX, remove("ssh"))
        self.assertIn("unparseable", msg.lower())
