"""jackil_ticket / jackil_note / jackil_resolve on the agent.

The handlers do no work here: a helpdesk ticket is server state. What is
checked is that each one refuses a task it cannot carry out, and reports the
text the server will act on.
"""

import sys
import unittest
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.config import AgentConfig
from vigil_agent.executor import _jackil_note, _jackil_resolve, _jackil_ticket


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control"}
    base.update(kw)
    return AgentConfig(**base)


class JackilTicketOutputTests(unittest.TestCase):
    def test_it_reports_the_title(self):
        out = _jackil_ticket({"title": "Backup failed", "key": "backup"}, _config())
        self.assertEqual(out.data, {"title": "Backup failed", "key": "backup"})
        self.assertEqual(
            str(out),
            "Jackil ticket requested: Backup failed — server will open or update it")

    def test_a_valid_priority_is_accepted(self):
        out = _jackil_ticket({"title": "t", "key": "k", "priority": "CRITICAL"},
                             _config())
        self.assertEqual(out.data["title"], "t")

    def test_an_unknown_priority_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_ticket({"title": "t", "key": "k", "priority": "urgent"},
                           _config())

    def test_a_missing_title_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_ticket({"key": "k"}, _config())

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_ticket({"title": "t"}, _config())

    def test_a_long_title_comes_back_capped(self):
        out = _jackil_ticket({"title": "x" * 3000, "key": "k"}, _config())
        self.assertEqual(len(out.data["title"]), 200)


class JackilNoteOutputTests(unittest.TestCase):
    def test_it_reports_the_body(self):
        out = _jackil_note({"key": "backup", "body": "retried, still failing"},
                           _config())
        self.assertEqual(out.data, {"body": "retried, still failing"})
        self.assertEqual(
            str(out),
            "Jackil note requested for backup — server will add it")

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_note({"body": "b"}, _config())

    def test_a_missing_body_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_note({"key": "k"}, _config())

    def test_a_blank_body_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_note({"key": "k", "body": "   "}, _config())

    def test_a_long_body_comes_back_capped(self):
        out = _jackil_note({"key": "k", "body": "x" * 5000}, _config())
        self.assertEqual(len(out.data["body"]), 4000)


class JackilResolveOutputTests(unittest.TestCase):
    def test_it_reports_the_key(self):
        out = _jackil_resolve({"key": "backup"}, _config())
        self.assertEqual(out.data, {"key": "backup"})
        self.assertEqual(
            str(out),
            "Jackil ticket resolve requested: backup — server will close it")

    def test_a_closed_status_is_accepted(self):
        out = _jackil_resolve({"key": "k", "status": "CLOSED"}, _config())
        self.assertEqual(out.data, {"key": "k"})

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_resolve({"key": "k", "status": "wontfix"}, _config())

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            _jackil_resolve({}, _config())


if __name__ == "__main__":
    unittest.main()
