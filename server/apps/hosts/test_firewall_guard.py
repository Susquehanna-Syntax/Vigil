"""The tab refuses changes that would cut off access to the host.

The task editor stays unguarded on purpose — that is the escape hatch. This
guard exists so a form cannot do by accident what a hand-written task can do
deliberately.

Rule actions in a snapshot are `allow` / `deny` / `reject` (lowercase), and
the removal tests that name a specific rule use `snapshot_with()` to put that
rule in the snapshot the guard checks. `remove()` stays the port-only shape
so the pre-existing "a missing action means allow" tests keep their meaning;
`remove_rule()` adds the `rule_id` the Firewall tab sends.
"""
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from apps.hosts.firewall_guard import check_change

LINUX = {"tool": "ufw", "enabled": True,
         "defaults": {"incoming": "deny", "outgoing": "allow"},
         "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                    "source": "any", "interface": ""}]}

WINDOWS = {"tool": "windows", "enabled": True,
           "defaults": {"incoming": "allow", "outgoing": "allow"},
           "rules": [{"port": 3389, "protocol": "tcp", "action": "allow",
                      "source": "any", "interface": "", "rule_id": "RDP-In"}]}

DENY_22 = {"port": 22, "protocol": "tcp", "action": "deny",
           "source": "10.0.0.5", "interface": "", "rule_id": ""}

REJECT_22 = {**DENY_22, "action": "reject"}


def snapshot_with(base, *rules):
    """A snapshot copy carrying extra rules, the way a real one does."""
    return {**base, "rules": list(base["rules"]) + [dict(r) for r in rules]}


class Host:
    def __init__(self, os="Ubuntu 24.04"):
        self.os = os


def windows():
    return Host(os="Windows Server 2022")


def add(port, action="allow", protocol="tcp"):
    return {"action": "add_firewall_rule",
            "params": {"port": port, "protocol": protocol, "action": action}}


def remove(port, protocol="tcp", action=None):
    params = {"port": port, "protocol": protocol}
    if action is not None:
        params["action"] = action
    return {"action": "remove_firewall_rule", "params": params}


def remove_rule(port, protocol="tcp", action=None, rule_id=None):
    """A removal that names the rule, the way the Firewall tab does."""
    params = {"port": port, "protocol": protocol}
    if action is not None:
        params["action"] = action
    if rule_id is not None:
        params["rule_id"] = rule_id
    return {"action": "remove_firewall_rule", "params": params}


def policy(direction, value):
    return {"action": "set_firewall_policy",
            "params": {"direction": direction, "policy": value}}


class LockoutGuardTests(TestCase):
    def setUp(self):
        self.host = Host()

    def test_an_ordinary_rule_is_allowed(self):
        self.assertEqual(check_change(self.host, LINUX, add(8080)), "")

    def test_denying_ssh_is_refused(self):
        r = check_change(self.host, LINUX, add(22, "deny"))
        self.assertIn("22", r)
        self.assertTrue(r)

    def test_removing_the_ssh_allow_rule_is_refused(self):
        self.assertNotEqual(check_change(self.host, LINUX, remove(22)), "")

    # -- removing a *block* is the way back in ------------------------------

    def test_removing_a_deny_on_ssh_is_allowed(self):
        self.assertEqual(
            check_change(self.host, snapshot_with(LINUX, DENY_22),
                         remove(22, action="deny")),
            "")

    def test_removing_a_reject_on_ssh_is_allowed(self):
        self.assertEqual(
            check_change(self.host, snapshot_with(LINUX, REJECT_22),
                         remove(22, action="reject")),
            "")

    def test_removing_a_block_on_rdp_is_allowed_on_windows(self):
        self.assertEqual(
            check_change(
                windows(),
                snapshot_with(WINDOWS, {"port": 3389, "protocol": "tcp",
                                        "action": "reject",
                                        "rule_id": "Block-RDP"}),
                remove_rule(3389, action="reject", rule_id="Block-RDP")),
            "")

    def test_removing_the_ssh_allow_is_still_refused_with_an_explicit_action(self):
        self.assertNotEqual(
            check_change(self.host, LINUX, remove(22, action="allow")), "")

    def test_a_claimed_deny_on_the_rdp_allow_is_refused(self):
        """The action comes from the browser and Windows removes by rule_id,
        so a claimed deny carrying the RDP *allow* rule's id is a lockout."""
        self.assertNotEqual(
            check_change(windows(), WINDOWS,
                         remove_rule(3389, action="deny", rule_id="RDP-In")),
            "")

    def test_a_block_missing_from_the_snapshot_is_refused(self):
        """Nothing to remove, so nothing to confirm the claim against — fail
        safe rather than wave an unverified action through."""
        self.assertNotEqual(
            check_change(self.host, LINUX, remove(22, action="deny")), "")

    def test_removing_a_block_on_an_unprotected_port_is_allowed(self):
        self.assertEqual(
            check_change(self.host, LINUX, remove(8080, action="deny")), "")

    # -- adding a block -----------------------------------------------------

    def test_rejecting_ssh_is_refused(self):
        r = check_change(self.host, LINUX, add(22, "reject"))
        self.assertTrue(r)
        self.assertIn("22", r)

    def test_rejecting_rdp_is_refused_on_windows(self):
        self.assertTrue(check_change(windows(), WINDOWS, add(3389, "reject")))

    def test_rejecting_an_unparseable_port_is_refused(self):
        self.assertTrue(check_change(self.host, LINUX, add("ssh", "reject")))

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


class FirewallUiMirrorTests(SimpleTestCase):
    """The Firewall tab decides which rules get a Remove button by mirroring
    this guard. When the two disagree the tab offers a change the server is
    guaranteed to refuse, so the mirror is checked here rather than by eye."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = (Path(__file__).resolve().parents[2]
                      / "static" / "js" / "vigil-firewall.js").read_text(
            encoding="utf-8")

    def test_the_protected_check_mirrors_the_allows_only_rule(self):
        """Only deny/reject are removable on a protected port — the guard
        refuses an allow, and a rule whose action could not be read."""
        self.assertIn("function _fwIsProtected(port, tool, action)",
                      self.source)
        self.assertIn("if (action === 'deny' || action === 'reject') return false;",
                      self.source)

    def test_reject_rules_are_no_longer_marked_unremovable(self):
        """The agent removes reject rules now, so the tab must offer the
        button instead of a dead label."""
        self.assertNotIn("[cannot remove]", self.source)

    def test_the_protected_label_names_the_service_it_keeps_open(self):
        for label in ("[protected — keeps SSH open]",
                      "[protected — keeps RDP open]"):
            self.assertIn(label, self.source)
