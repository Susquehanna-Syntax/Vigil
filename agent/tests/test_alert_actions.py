"""raise_alert / resolve_alert / send_notification on the agent.

The handlers do no work here: an alert is server state. What is checked is that
each one refuses a task it cannot carry out, and reports the text the server
will act on.
"""

import sys
import unittest
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.config import AgentConfig
from vigil_agent.executor import _raise_alert, _resolve_alert, _send_notification


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control"}
    base.update(kw)
    return AgentConfig(**base)


class RaiseAlertOutputTests(unittest.TestCase):
    def test_it_reports_the_message(self):
        out = _raise_alert({"severity": "warning", "message": "disk full"}, _config())
        self.assertEqual(out.data, {"message": "disk full"})
        self.assertEqual(str(out),
                         "Alert requested: warning: disk full — server will raise it")

    def test_severity_is_case_insensitive_and_reported_lower(self):
        out = _raise_alert({"severity": "CRITICAL", "message": "m"}, _config())
        self.assertEqual(str(out), "Alert requested: critical: m — server will raise it")

    def test_a_missing_message_is_refused(self):
        with self.assertRaises(ValueError):
            _raise_alert({"severity": "warning"}, _config())

    def test_an_empty_message_is_refused(self):
        with self.assertRaises(ValueError):
            _raise_alert({"severity": "warning", "message": "   "}, _config())

    def test_a_missing_severity_is_refused(self):
        with self.assertRaises(ValueError):
            _raise_alert({"message": "m"}, _config())

    def test_an_unknown_severity_is_refused(self):
        with self.assertRaises(ValueError):
            _raise_alert({"severity": "loud", "message": "m"}, _config())

    def test_a_long_message_comes_back_capped(self):
        out = _raise_alert({"severity": "info", "message": "x" * 3000}, _config())
        self.assertEqual(len(out.data["message"]), 2000)


class ResolveAlertOutputTests(unittest.TestCase):
    def test_it_reports_the_key(self):
        out = _resolve_alert({"key": "disk-cleanup"}, _config())
        self.assertEqual(out.data, {"key": "disk-cleanup"})
        self.assertEqual(str(out),
                         "Alert resolve requested: disk-cleanup — server will resolve it")

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            _resolve_alert({}, _config())

    def test_a_blank_key_is_refused(self):
        with self.assertRaises(ValueError):
            _resolve_alert({"key": "  "}, _config())


class SendNotificationOutputTests(unittest.TestCase):
    def test_it_reports_the_message(self):
        out = _send_notification({"message": "sweep done"}, _config())
        self.assertEqual(out.data, {"message": "sweep done"})
        self.assertEqual(str(out), "Notification requested — server will send it")

    def test_a_missing_message_is_refused(self):
        with self.assertRaises(ValueError):
            _send_notification({}, _config())

    def test_a_long_message_comes_back_capped(self):
        out = _send_notification({"message": "y" * 3000}, _config())
        self.assertEqual(len(out.data["message"]), 2000)


class HandlerTableTests(unittest.TestCase):
    def test_the_executor_routes_all_three(self):
        from vigil_agent import executor
        self.assertIs(executor._HANDLERS["raise_alert"], _raise_alert)
        self.assertIs(executor._HANDLERS["resolve_alert"], _resolve_alert)
        self.assertIs(executor._HANDLERS["send_notification"], _send_notification)

    def test_the_names_are_allowlistable(self):
        from vigil_agent.config import _ALL_ACTIONS
        self.assertTrue({"raise_alert", "resolve_alert",
                         "send_notification"} <= _ALL_ACTIONS)


if __name__ == "__main__":
    unittest.main()
