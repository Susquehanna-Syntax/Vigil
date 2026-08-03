from django.test import TestCase

from apps.tasks.spec import ACTION_REGISTRY
from vigil import hooks

REPROVISION_ACTIONS = {
    "reprovision_stage", "reprovision_commit", "reprovision_cleanup",
}


class ActionRegistryTests(TestCase):
    def test_all_four_actions_registered(self):
        for action in REPROVISION_ACTIONS | {"reprovision_preflight"}:
            self.assertIn(action, ACTION_REGISTRY)

    def test_destructive_actions_are_high_risk(self):
        for action in REPROVISION_ACTIONS:
            self.assertEqual(ACTION_REGISTRY[action]["risk"], "high", action)

    def test_preflight_is_low_risk(self):
        # Read-only, so an operator can survey readiness without a 2FA gate.
        self.assertEqual(ACTION_REGISTRY["reprovision_preflight"]["risk"], "low")

    def test_stage_requires_the_params_the_agent_needs(self):
        required = set(ACTION_REGISTRY["reprovision_stage"]["required"])
        self.assertEqual(required, {"job_id", "kernel_url", "initrd_url",
                                    "kernel_sha256", "initrd_sha256"})

    def test_commit_requires_the_kernel_cmdline(self):
        required = set(ACTION_REGISTRY["reprovision_commit"]["required"])
        self.assertEqual(required, {"job_id", "cmdline"})

    def test_registry_mirrors_the_agent_carve_out(self):
        """spec.py's own docstring requires lockstep with the agent executor.

        The agent gates exactly these three on allow_reprovision; if the two
        lists drift, the server will offer an action the agent silently
        refuses, or worse, treat a destructive action as ordinary.
        """
        import sys
        from pathlib import Path

        agent_dir = Path(__file__).resolve().parents[3] / "agent"
        sys.path.insert(0, str(agent_dir))
        try:
            from vigil_agent.config import REPROVISION_ACTIONS as AGENT_SET
        finally:
            sys.path.remove(str(agent_dir))

        self.assertEqual(set(AGENT_SET), REPROVISION_ACTIONS)


class HookEventTests(TestCase):
    def test_reprovision_events_declared(self):
        for event in ("rebuild_requested", "rebuild_state_changed",
                      "rebuild_answer_fetched", "rebuild_completed"):
            self.assertIn(event, hooks.KNOWN_EVENTS)
