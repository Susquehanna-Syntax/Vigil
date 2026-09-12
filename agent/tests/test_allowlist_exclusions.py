"""Entries that cannot be allowlisted must be dropped, not just complained about.

Two different things end up "ignored" in an allowlist, and they deserve
different messages:

  * a typo, or a config written for a newer agent — the entry is not an action
    at all;
  * a real action that is deliberately not allowlistable. run_command executes
    arbitrary commands and needs mode: full_control; the destructive
    reprovision actions are granted by allow_reprovision instead.

Telling an operator to look for a typo in `run_command` sends them hunting for
something that is not there. But the split is only cosmetic — both sets must
still leave the allowlist. Splitting the warning once split the enforcement
too, leaving run_command allowlisted so a managed-mode agent would have
accepted arbitrary command execution.
"""

import unittest

from vigil_agent.config import AgentConfig, _NEVER_ALLOWLISTABLE


def _cfg(**kw):
    base = dict(server_url="http://x", agent_token="t" * 40, mode="managed")
    base.update(kw)
    return AgentConfig(**base)


class NeverAllowlistable(unittest.TestCase):
    def test_run_command_is_dropped_from_the_allowlist(self):
        c = _cfg(allowlist={"run_command", "restart_service"})
        self.assertNotIn("run_command", c.allowlist)
        self.assertIn("restart_service", c.allowlist)

    def test_run_command_is_refused_in_managed_mode(self):
        """The enforcement, not just the bookkeeping."""
        c = _cfg(allowlist={"run_command"})
        self.assertFalse(c.task_allowed("run_command"))

    def test_full_control_still_grants_run_command(self):
        c = _cfg(mode="full_control", allowlist=set())
        self.assertTrue(c.task_allowed("run_command"))

    def test_destructive_reprovision_actions_are_dropped(self):
        c = _cfg(allowlist={"reprovision_commit", "reprovision_stage"})
        self.assertEqual(c.allowlist, set())
        self.assertFalse(c.task_allowed("reprovision_commit"))

    def test_reprovision_needs_the_flag_not_the_allowlist(self):
        c = _cfg(allow_reprovision=True, allowlist=set())
        self.assertTrue(c.task_allowed("reprovision_commit"))

    def test_typos_are_dropped_too(self):
        c = _cfg(allowlist={"totally_made_up", "restart_service"})
        self.assertEqual(c.allowlist, {"restart_service"})

    def test_the_two_categories_are_reported_separately(self):
        with self.assertLogs("vigil.config", level="WARNING") as cm:
            _cfg(allowlist={"run_command", "totally_made_up"})
        joined = "\n".join(cm.output)
        self.assertIn("cannot be allowlisted", joined)
        self.assertIn("full_control", joined)
        self.assertIn("typo", joined)

    def test_every_never_allowlistable_entry_is_actually_refused(self):
        """Guards the constant against drifting away from the enforcement."""
        for action in _NEVER_ALLOWLISTABLE:
            c = _cfg(allowlist={action})
            self.assertNotIn(action, c.allowlist, action)
            self.assertFalse(c.task_allowed(action), action)


if __name__ == "__main__":
    unittest.main()
