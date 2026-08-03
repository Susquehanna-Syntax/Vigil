"""The allow_reprovision carve-out (docs/reprovisioning.md §4.1).

full_control means "run any action the server asks for". Reprovisioning is
excluded from that promise: a compromised server must not be able to order a
fleet to wipe itself. These tests are the executable form of that rule.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.config import REPROVISION_ACTIONS, AgentConfig


def _config(**kwargs) -> AgentConfig:
    base = dict(server_url="https://vigil.example.com", agent_token="t")
    base.update(kwargs)
    return AgentConfig(**base)


class ReprovisionGateTests(unittest.TestCase):
    def test_full_control_alone_does_not_permit_reprovision(self):
        cfg = _config(mode="full_control")
        for action in REPROVISION_ACTIONS:
            self.assertFalse(
                cfg.task_allowed(action),
                f"{action} must not be implied by full_control",
            )

    def test_opt_in_permits_reprovision_in_full_control(self):
        cfg = _config(mode="full_control", allow_reprovision=True)
        for action in REPROVISION_ACTIONS:
            self.assertTrue(cfg.task_allowed(action))

    def test_opt_in_permits_reprovision_in_managed_without_allowlist(self):
        # The flag is the whole gate: it does not also need an allowlist entry.
        cfg = _config(mode="managed", allowlist=set(), allow_reprovision=True)
        for action in REPROVISION_ACTIONS:
            self.assertTrue(cfg.task_allowed(action))

    def test_allowlisting_cannot_substitute_for_the_flag(self):
        cfg = _config(mode="managed", allowlist=set(REPROVISION_ACTIONS))
        for action in REPROVISION_ACTIONS:
            self.assertFalse(
                cfg.task_allowed(action),
                f"{action} must not be reachable via the allowlist alone",
            )

    def test_monitor_mode_refuses_even_with_the_flag(self):
        # Monitor mode executes nothing, full stop. The flag does not upgrade it.
        cfg = _config(mode="monitor", allow_reprovision=True)
        for action in REPROVISION_ACTIONS:
            self.assertFalse(cfg.task_allowed(action))

    def test_preflight_is_not_gated_by_the_flag(self):
        # Read-only: allowlistable like any other low-risk action.
        cfg = _config(mode="managed", allowlist={"reprovision_preflight"})
        self.assertTrue(cfg.task_allowed("reprovision_preflight"))

    def test_preflight_is_not_in_the_carve_out(self):
        self.assertNotIn("reprovision_preflight", REPROVISION_ACTIONS)

    def test_default_is_off(self):
        self.assertFalse(_config().allow_reprovision)

    def test_flag_is_read_from_yaml(self):
        import tempfile

        from vigil_agent.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yml"
            path.write_text(
                "server_url: https://vigil.example.com\n"
                "agent_token: t\n"
                "mode: managed\n"
                "allow_reprovision: true\n"
            )
            path.chmod(0o600)
            cfg = load_config(path)
        self.assertTrue(cfg.allow_reprovision)

    def test_absent_from_yaml_means_off(self):
        import tempfile

        from vigil_agent.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yml"
            path.write_text(
                "server_url: https://vigil.example.com\n"
                "agent_token: t\n"
                "mode: full_control\n"
            )
            path.chmod(0o600)
            cfg = load_config(path)
        self.assertFalse(cfg.allow_reprovision)


if __name__ == "__main__":
    unittest.main()
