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

    def test_a_name_param_does_not_change_the_ufw_command(self):
        # No regression: ufw removes by field match, not identity, so a
        # `name` param passed through from the UI must be silently ignored
        # here rather than changing what ufw is asked to do.
        cmd = self._cmd({"port": 80, "protocol": "tcp", "name": "irrelevant"})
        self.assertNotIn("irrelevant", cmd)
        self.assertEqual(cmd, ["ufw", "delete", "allow", "80/tcp"])


class RemoveRuleNameThreadingTests(unittest.TestCase):
    """executor._remove_firewall_rule must pass the caller's `name` AND
    `rule_id` params through to backend.remove_rule -- WindowsBackend
    removes by `rule_id` (the unique identifier), not `name` (DisplayName,
    not guaranteed unique)."""

    def test_name_and_rule_id_reach_the_backend(self):
        seen = {}

        class RecordingBackend:
            name = "windows"

            def remove_rule(self, port, protocol, action="allow",
                            source="any", name="", rule_id=""):
                seen["args"] = (port, protocol, action, source, name, rule_id)
                return "ok"

        with patch.object(firewall, "detect", lambda: RecordingBackend()):
            executor._remove_firewall_rule(
                {"port": 3389, "protocol": "tcp", "name": "RDP",
                 "rule_id": "{22222222-2222-2222-2222-222222222222}"},
                _config())

        self.assertEqual(
            seen["args"],
            (3389, "tcp", "allow", "any", "RDP",
             "{22222222-2222-2222-2222-222222222222}"))

    def test_missing_name_and_rule_id_reach_the_backend_as_empty_strings(self):
        seen = {}

        class RecordingBackend:
            name = "windows"

            def remove_rule(self, port, protocol, action="allow",
                            source="any", name="", rule_id=""):
                seen["name"] = name
                seen["rule_id"] = rule_id
                return "ok"

        with patch.object(firewall, "detect", lambda: RecordingBackend()):
            executor._remove_firewall_rule(
                {"port": 3389, "protocol": "tcp"}, _config())

        self.assertEqual(seen["name"], "")
        self.assertEqual(seen["rule_id"], "")


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

    def test_a_deny_rule_is_removed_with_the_same_rich_rule_it_was_added_with(self):
        # Finding 1 (critical): the deny path in add_rule writes a reject
        # rich rule, not a port grant. If remove_rule ignores `action` and
        # always runs --remove-port, the rich rule is never touched and the
        # removal is a silent no-op reported as success -- the exact bug
        # this task exists to fix, reintroduced in the sibling backend.
        add_calls = self._cmds(executor._add_firewall_rule,
                               {"port": 8080, "protocol": "tcp", "action": "deny"})
        remove_calls = self._cmds(executor._remove_firewall_rule,
                                  {"port": 8080, "protocol": "tcp", "action": "deny"})

        added_rule = next(c for c in add_calls
                          if any("--add-rich-rule=" in tok for tok in c))
        added_rich_rule = next(tok for tok in added_rule
                               if tok.startswith("--add-rich-rule="))[len("--add-rich-rule="):]

        removed_joined = [" ".join(c) for c in remove_calls]
        self.assertTrue(
            any(f"--remove-rich-rule={added_rich_rule}" in c
                for c in removed_joined),
            "the removed rich rule must be the exact one add_rule wrote")
        self.assertFalse(any("--remove-port" in c for c in removed_joined))

    def test_an_allow_rule_is_still_removed_with_remove_port(self):
        calls = self._cmds(executor._remove_firewall_rule,
                           {"port": 8080, "protocol": "tcp", "action": "allow"})
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("--remove-port=8080/tcp" in c for c in joined))
        self.assertFalse(any("--remove-rich-rule" in c for c in joined))

    def test_a_deny_removal_still_reloads(self):
        calls = self._cmds(executor._remove_firewall_rule,
                           {"port": 8080, "protocol": "tcp", "action": "deny"})
        self.assertTrue(any("--reload" in c for c in calls))

    def test_a_name_param_does_not_change_the_firewall_cmd_removal(self):
        # No regression: firewall-cmd removes by field match (port or rich
        # rule text), not identity, so a `name` param passed through from
        # the UI must be silently ignored here too.
        with_name = self._cmds(
            executor._remove_firewall_rule,
            {"port": 8080, "protocol": "tcp", "action": "allow", "name": "irrelevant"})
        without_name = self._cmds(
            executor._remove_firewall_rule,
            {"port": 8080, "protocol": "tcp", "action": "allow"})
        self.assertEqual(with_name, without_name)
        self.assertFalse(any("irrelevant" in " ".join(c) for c in with_name))


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


class InterfaceValidationTests(unittest.TestCase):
    def test_validate_interface_accepts_a_plain_name(self):
        self.assertEqual(firewall.validate_interface("eth0"), "eth0")

    def test_validate_interface_rejects_a_shell_metacharacter(self):
        with self.assertRaises(ValueError):
            firewall.validate_interface("eth0; rm -rf /")

    def test_validate_interface_rejects_an_overlong_name(self):
        with self.assertRaises(ValueError):
            firewall.validate_interface("a" * 17)


class UfwInterfaceTests(unittest.TestCase):
    def test_an_interface_with_no_source_uses_the_full_form(self):
        # Finding 3: ufw's documented syntax pairs `in on IFACE` with the
        # full `to any port PORT proto PROTO` form. Mixing `in on IFACE`
        # with the short PORT/PROTO form does not reliably parse.
        backend = firewall.UfwBackend()
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        with patch.object(firewall, "_run", fake_run):
            backend.add_rule(22, "tcp", "allow", source="any", interface="eth0")

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("in", cmd)
        self.assertIn("on", cmd)
        self.assertIn("eth0", cmd)
        joined = " ".join(cmd)
        self.assertIn("to any port 22 proto tcp", joined)
        self.assertNotIn("22/tcp", cmd)


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

    def test_add_rule_gives_an_allow_and_a_deny_different_displaynames(self):
        # CRITICAL (review, task-12 follow-up): the old naming scheme
        # (`f"Vigil {protocol}/{port}"`) named every rule on a port
        # identically regardless of action or source, so an allow and a
        # later scoped deny on the same port collided on DisplayName --
        # exactly the allow+scoped-deny pattern the removal fix's hand-trace
        # used as its motivating example. Defense in depth only: removal
        # itself must still key on rule_id, never on this name, but a
        # naming scheme that *guarantees* a collision is its own latent
        # trap.
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.add_rule(8443, "tcp", "allow")
            backend.add_rule(8443, "tcp", "deny", source="203.0.113.9")

        allow_name = next(tok for tok in calls[0] if tok.startswith("name="))
        deny_name = next(tok for tok in calls[1] if tok.startswith("name="))
        self.assertNotEqual(allow_name, deny_name)
        self.assertIn("allow", allow_name)
        self.assertIn("deny", deny_name)


class WindowsRemoveByRuleIdTests(unittest.TestCase):
    """Task 12 (High) + CRITICAL follow-up: netsh has no action filter, so
    `delete rule name=all dir=in protocol=X localport=Y` deletes EVERY
    inbound rule on that port -- allows and denies alike. A port carrying a
    general allow plus a scoped deny loses both on one Remove, and under a
    default-allow inbound policy that re-permits the previously-blocked
    source.

    The first fix removed by DisplayName -- but DisplayName is NOT
    guaranteed unique, and WindowsBackend.add_rule's own (then-)naming
    scheme guaranteed a collision for exactly the allow+scoped-deny pattern
    above (both named "Vigil tcp/<port>"). remove_rule must therefore
    remove by the rule's unique Name (InstanceID, `rule_id`) via
    PowerShell, and must refuse outright -- never fall back to the old
    netsh command, and never fall back to DisplayName -- when no rule_id is
    given."""

    def test_a_rule_id_issues_remove_netfirewallrule_by_name(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.remove_rule(
                3389, "tcp", rule_id="{22222222-2222-2222-2222-222222222222}")

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        # Single argv element carrying the PowerShell command, per the
        # existing _PS list-argv convention in firewall.py.
        joined = " ".join(cmd)
        self.assertIn("Remove-NetFirewallRule", joined)
        self.assertIn("-Name", joined)
        self.assertNotIn("-DisplayName", joined)
        self.assertIn("{22222222-2222-2222-2222-222222222222}", joined)

    def test_removing_by_rule_id_never_issues_a_netsh_port_match(self):
        # This is the over-match the original fix closed: the old command
        # was `netsh advfirewall firewall delete rule name=all dir=in
        # protocol=<P> localport=<N>`, which cannot distinguish an allow
        # rule on a port from a deny rule on the same port.
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.remove_rule(3389, "tcp", rule_id="{2222}")

        for cmd in calls:
            self.assertNotIn("netsh", cmd)
            self.assertNotIn("name=all", " ".join(cmd))

    def test_a_name_with_no_rule_id_still_refuses(self):
        # This is the CRITICAL the review found: the first fix accepted a
        # `name` (DisplayName) alone as sufficient. It is not -- DisplayName
        # collides by construction for rules add_rule itself creates. A
        # DisplayName with no rule_id must still refuse, not silently fall
        # back to -DisplayName.
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            with self.assertRaises(RuntimeError):
                backend.remove_rule(3389, "tcp", name="Vigil tcp/3389 allow")

        self.assertEqual(calls, [], "must refuse before running anything")

    def test_no_rule_id_refuses_rather_than_falling_back_to_netsh(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                backend.remove_rule(3389, "tcp")

        self.assertEqual(calls, [], "must refuse before running anything")
        message = str(ctx.exception)
        self.assertIn("identifier", message.lower())
        self.assertIn("netsh", message.lower())

    def test_a_blank_rule_id_also_refuses(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(RuntimeError):
                backend.remove_rule(3389, "tcp", rule_id="   ")

    def test_a_rule_id_with_a_backtick_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id="Bad`Id")

    def test_a_rule_id_with_a_semicolon_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id="Bad;Id")

    def test_a_rule_id_with_a_double_quote_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id='Bad"Id')

    def test_a_rule_id_with_a_single_quote_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id="Bad'Id")

    def test_a_rule_id_with_a_dollar_sign_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id="Bad$Id")

    def test_an_overlong_rule_id_is_refused(self):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", lambda cmd, timeout=None: "ok"):
            with self.assertRaises(ValueError):
                backend.remove_rule(3389, "tcp", rule_id="x" * 256)

    def test_a_realistic_guid_rule_id_is_accepted(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.remove_rule(
                5355, "udp",
                rule_id="{6D5F1E2A-9C3B-4E7A-BF2D-1A2B3C4D5E6F}")

        self.assertEqual(len(calls), 1)
        self.assertIn(
            "{6D5F1E2A-9C3B-4E7A-BF2D-1A2B3C4D5E6F}",
            " ".join(calls[0]))

    def test_two_same_port_rules_sharing_a_displayname_remove_independently(self):
        # The regression test for the Critical itself: an allow and a
        # scoped deny on the same port, sharing the identical DisplayName
        # "Vigil tcp/8443" (exactly what the pre-fix add_rule naming scheme
        # produced), must still be removable independently by rule_id --
        # and the argv for each must target only the one selected, never
        # both, never the other rule's identifier.
        allow_id = "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}"
        deny_id = "{bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb}"
        shared_display_name = "Vigil tcp/8443"

        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.remove_rule(8443, "tcp", action="allow",
                                name=shared_display_name, rule_id=allow_id)
            backend.remove_rule(8443, "tcp", action="deny",
                                name=shared_display_name, rule_id=deny_id)

        self.assertEqual(len(calls), 2)
        allow_call, deny_call = " ".join(calls[0]), " ".join(calls[1])

        self.assertIn(allow_id, allow_call)
        self.assertNotIn(deny_id, allow_call)

        self.assertIn(deny_id, deny_call)
        self.assertNotIn(allow_id, deny_call)

        # Neither call names the (shared, ambiguous) DisplayName as the
        # match target -- both key exclusively on -Name.
        self.assertNotIn("-DisplayName", allow_call)
        self.assertNotIn("-DisplayName", deny_call)


class RuleNameValidationTests(unittest.TestCase):
    def test_validate_rule_name_accepts_a_plain_name(self):
        self.assertEqual(firewall.validate_rule_name("OpenSSH"), "OpenSSH")

    def test_validate_rule_name_rejects_empty(self):
        with self.assertRaises(ValueError):
            firewall.validate_rule_name("")

    def test_validate_rule_name_rejects_a_newline(self):
        with self.assertRaises(ValueError):
            firewall.validate_rule_name("Vigil\nRule")

    def test_validate_rule_name_rejects_a_pipe(self):
        with self.assertRaises(ValueError):
            firewall.validate_rule_name("Vigil|Rule")

    def test_validate_rule_name_rejects_an_ampersand(self):
        with self.assertRaises(ValueError):
            firewall.validate_rule_name("Vigil&Rule")


class PolicyAndToggleTests(unittest.TestCase):
    def _ufw(self, fn, params):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        with patch.object(firewall, "_run", fake_run), \
             patch.object(firewall.shutil, "which",
                          lambda t: "/usr/sbin/ufw" if t == "ufw" else None):
            fn(params, _config())
        return calls

    def test_incoming_policy_is_set(self):
        calls = self._ufw(executor._set_firewall_policy,
                          {"direction": "incoming", "policy": "deny"})
        self.assertIn(["ufw", "default", "deny", "incoming"], calls)

    def test_outgoing_policy_is_set(self):
        calls = self._ufw(executor._set_firewall_policy,
                          {"direction": "outgoing", "policy": "allow"})
        self.assertIn(["ufw", "default", "allow", "outgoing"], calls)

    def test_a_bogus_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            executor._set_firewall_policy(
                {"direction": "sideways", "policy": "deny"}, _config())

    def test_a_bogus_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            executor._set_firewall_policy(
                {"direction": "incoming", "policy": "maybe"}, _config())

    def test_enable(self):
        calls = self._ufw(executor._enable_firewall, {})
        self.assertIn(["ufw", "--force", "enable"], calls)

    def test_disable(self):
        calls = self._ufw(executor._disable_firewall, {})
        self.assertIn(["ufw", "disable"], calls)

    def test_all_three_are_registered(self):
        # The dispatch table is named `_HANDLERS` in executor.py, not
        # `ACTION_HANDLERS` -- see the note in ListRulesTests above.
        for a in ("set_firewall_policy", "enable_firewall", "disable_firewall"):
            self.assertIn(a, executor._HANDLERS, a)

    def test_all_three_are_allowlistable(self):
        from vigil_agent.config import _ALL_ACTIONS
        for a in ("set_firewall_policy", "enable_firewall", "disable_firewall"):
            self.assertIn(a, _ALL_ACTIONS, a)


class FirewallCmdPolicyTests(unittest.TestCase):
    """firewalld has no separate outgoing default policy -- set_policy must
    refuse that direction rather than silently doing nothing."""

    def test_outgoing_is_rejected(self):
        backend = firewall.FirewallCmdBackend()
        with self.assertRaises(ValueError):
            backend.set_policy("outgoing", "deny")

    def test_incoming_sets_the_target_and_reloads(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return "ok"

        backend = firewall.FirewallCmdBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.set_policy("incoming", "deny")

        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("--set-target=DROP" in c for c in joined))
        self.assertTrue(any("--reload" in c for c in joined))


class WindowsPolicyPreservesOtherDirectionTests(unittest.TestCase):
    """The brief's WindowsBackend.set_policy hardcodes the direction it was
    not asked to change (`allowoutbound` when setting inbound, `blockinbound`
    when setting outbound). netsh's `firewallpolicy` setter changes both
    directions in one call, so that hardcoding silently resets whichever
    direction the caller didn't touch. The fix reads the host's real current
    policy for the other direction first and restates it unchanged."""

    def _set_policy(self, profiles_json, direction, policy):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            # First call reads current defaults (Get-NetFirewallProfile);
            # everything after is the netsh policy-setting call.
            if "Get-NetFirewallProfile" in cmd[-1]:
                return profiles_json
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.set_policy(direction, policy)
        return calls

    def test_setting_inbound_preserves_an_existing_outbound_block(self):
        # Outbound is deliberately Block, not the hardcoded "allowoutbound"
        # the brief's buggy code would emit -- this is the case that catches
        # the bug.
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Block"},
        ])
        calls = self._set_policy(profiles, "incoming", "allow")
        set_cmd = calls[-1]
        joined = " ".join(set_cmd)
        self.assertIn("allowinbound", joined)
        self.assertIn("blockoutbound", joined,
                       "setting the inbound policy must not reset outbound")

    def test_setting_outbound_preserves_an_existing_inbound_allow(self):
        # Inbound is deliberately Allow, not the hardcoded "blockinbound"
        # the brief's buggy code would emit.
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Allow"},
        ])
        calls = self._set_policy(profiles, "outgoing", "deny")
        set_cmd = calls[-1]
        joined = " ".join(set_cmd)
        self.assertIn("allowinbound", joined,
                       "setting the outbound policy must not reset inbound")
        self.assertIn("blockoutbound", joined)


class WindowsPolicyRefusesOnUnknownOtherDirectionTests(unittest.TestCase):
    """Finding 1 (critical, task-6 review): falling back to Windows' own
    out-of-box default when the untouched direction can't be read is itself
    a security-loosening guess -- a host whose real outbound policy is a
    deliberate `deny`, hit by a transient read failure, would have it
    silently reset to `allow` by a call that only asked to change inbound.
    set_policy must refuse rather than guess when the direction it was NOT
    asked to change reads "unknown"."""

    def _refuses(self, run_fn, direction, policy):
        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", run_fn):
            with self.assertRaises(RuntimeError) as ctx:
                backend.set_policy(direction, policy)
        return str(ctx.exception)

    def test_setting_inbound_refuses_when_profiles_disagree_on_outbound(self):
        # Profiles disagree on DefaultOutboundAction -- _read_defaults
        # reports "unknown" for outgoing.
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Block"},
            {"Name": "Public", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Allow"},
        ])

        def fake_run(cmd, timeout=None):
            if "Get-NetFirewallProfile" in cmd[-1]:
                return profiles
            self.fail("netsh must not be invoked when the other direction "
                      "is unknown")

        message = self._refuses(fake_run, "incoming", "deny")
        self.assertIn("outgoing", message)

    def test_setting_inbound_refuses_when_the_profile_read_fails_outright(self):
        def fake_run(cmd, timeout=None):
            if "Get-NetFirewallProfile" in cmd[-1]:
                return "not valid json"
            self.fail("netsh must not be invoked when the profile read failed")

        message = self._refuses(fake_run, "incoming", "deny")
        self.assertIn("outgoing", message)

    def test_setting_outbound_refuses_when_profiles_disagree_on_inbound(self):
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Block", "DefaultOutboundAction": "Allow"},
            {"Name": "Public", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Allow"},
        ])

        def fake_run(cmd, timeout=None):
            if "Get-NetFirewallProfile" in cmd[-1]:
                return profiles
            self.fail("netsh must not be invoked when the other direction "
                      "is unknown")

        message = self._refuses(fake_run, "outgoing", "allow")
        self.assertIn("incoming", message)

    def test_the_known_good_preservation_paths_still_work(self):
        # No-regression check alongside the refusal tests above: a clean,
        # agreeing profile read must still succeed and preserve the other
        # direction, exactly as WindowsPolicyPreservesOtherDirectionTests
        # already covers.
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Block"},
        ])
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if "Get-NetFirewallProfile" in cmd[-1]:
                return profiles
            return "ok"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            backend.set_policy("incoming", "allow")

        joined = " ".join(calls[-1])
        self.assertIn("allowinbound", joined)
        self.assertIn("blockoutbound", joined)


class WindowsRealDefaultsSnapshotTests(unittest.TestCase):
    """Task 3 shipped WindowsBackend.snapshot() returning a literal
    {"incoming": "deny", "outgoing": "allow"} regardless of the host's real
    configuration. Now that policy is writable, that hardcoded lie would
    render a policy just set as unchanged."""

    def _snapshot(self, profiles_json, rules_json="[]"):
        def fake_run(cmd, timeout=None):
            if "Get-NetFirewallProfile" in cmd[-1]:
                return profiles_json
            return rules_json

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            return backend.snapshot()

    def test_defaults_reflect_the_real_profile_values_not_hardcoded(self):
        # The old hardcoded value was {"incoming": "deny", "outgoing":
        # "allow"} -- deliberately using "deny" for outgoing here so a
        # regression back to the hardcoded literal is caught.
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Block", "DefaultOutboundAction": "Block"},
        ])
        snap = self._snapshot(profiles)
        self.assertEqual(snap["defaults"],
                         {"incoming": "deny", "outgoing": "deny"})

    def test_disagreeing_profiles_report_unknown_for_that_direction(self):
        profiles = json.dumps([
            {"Name": "Domain", "Enabled": True,
             "DefaultInboundAction": "Block", "DefaultOutboundAction": "Allow"},
            {"Name": "Public", "Enabled": True,
             "DefaultInboundAction": "Allow", "DefaultOutboundAction": "Allow"},
        ])
        snap = self._snapshot(profiles)
        self.assertEqual(snap["defaults"],
                         {"incoming": "unknown", "outgoing": "allow"})

    def test_a_failed_profile_read_reports_unknown_for_both_directions(self):
        def fake_run(cmd, timeout=None):
            return "not valid json"

        backend = firewall.WindowsBackend()
        with patch.object(firewall, "_run", fake_run):
            snap = backend.snapshot()
        self.assertEqual(snap["defaults"],
                         {"incoming": "unknown", "outgoing": "unknown"})
        # Consistent with the tri-state `enabled` this backend already
        # returns: a failed read must never render as a known value.
        self.assertIsNone(snap["enabled"])
