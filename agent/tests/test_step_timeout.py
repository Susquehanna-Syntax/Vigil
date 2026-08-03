"""A step-level timeout must actually reach the handler (GitHub #16).

execute_action accepted a `timeout` keyword and then called
`handler(params, config)`, dropping it. runtime.py documented the key and
the server shipped it, so a long build still died at the 120s default with
nothing anywhere to explain why.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t",
                mode="full_control")
    base.update(kw)
    return AgentConfig(**base)


class StepTimeoutTests(unittest.TestCase):
    def _timeout_seen(self, params, **kwargs):
        seen = {}

        def fake_run(cmd, timeout=None):
            seen["timeout"] = timeout
            return "ok"

        with patch.object(executor, "_run", fake_run):
            executor.execute_action("run_command", params, _config(), **kwargs)
        return seen["timeout"]

    def test_step_timeout_reaches_the_handler(self):
        self.assertEqual(
            self._timeout_seen({"command": "sleep 1"}, timeout=1800), 1800)

    def test_explicit_params_timeout_wins(self):
        # The more specific of the two: a params timeout was set deliberately
        # on this action, the step timeout may be a blanket default.
        self.assertEqual(
            self._timeout_seen({"command": "x", "timeout": 600}, timeout=1800),
            600)

    def test_default_applies_when_nothing_is_given(self):
        self.assertEqual(
            self._timeout_seen({"command": "x"}), executor._EXEC_TIMEOUT)

    def test_timeout_is_not_injected_when_absent(self):
        # Handlers that take no timeout must not suddenly receive one.
        captured = {}

        def fake_handler(params, config):
            captured.update(params)
            return "ok"

        with patch.dict(executor._HANDLERS, {"check_service": fake_handler}):
            executor.execute_action("check_service", {"service_name": "x"},
                                    _config())
        self.assertNotIn("timeout", captured)

    def test_step_timeout_is_visible_to_any_handler(self):
        captured = {}

        def fake_handler(params, config):
            captured.update(params)
            return "ok"

        with patch.dict(executor._HANDLERS, {"check_service": fake_handler}):
            executor.execute_action("check_service", {"service_name": "x"},
                                    _config(), timeout=900)
        self.assertEqual(captured["timeout"], 900)

    def test_caller_params_are_not_mutated(self):
        params = {"command": "x"}
        with patch.object(executor, "_run", lambda cmd, timeout=None: "ok"):
            executor.execute_action("run_command", params, _config(),
                                    timeout=900)
        self.assertNotIn("timeout", params)


if __name__ == "__main__":
    unittest.main()
