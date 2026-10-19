"""``jackil_ticket`` / ``jackil_note`` / ``jackil_resolve`` on the server side.

Marker actions in QA-38's shape: the agent reports the step and the server
works the ticket when the result arrives. What the ticket is comes from the
signed task — key, priority, status — and only the free text comes from the
agent's report. The dedupe is the link row: one open ticket per host and key,
so a nightly playbook notes last night's ticket instead of opening another.

Nothing here talks to a real Jackil — the client is patched at the boundary.
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.instance import config
from apps.jackil import client, task_actions
from apps.jackil.models import JackilWorkTicket
from apps.tasks.models import Task

_ENABLED = "apps.jackil.task_actions.client.enabled"
_CREATE = "apps.jackil.task_actions.client.create_ticket"
_NOTE = "apps.jackil.task_actions.client.add_note"
_STATUS = "apps.jackil.task_actions.client.set_status"
_GET = "apps.jackil.task_actions.client.get_ticket"


def _turn_on():
    config.write("JACKIL_ENABLED", True)
    config.write("JACKIL_URL", "https://help.example.com")
    config.write("JACKIL_API_KEY", "key-123")


def _ticket_step(step_id="a", key="backup", title="signed title",
                 description=None, priority=None):
    params = {"title": title, "key": key}
    if description is not None:
        params["description"] = description
    if priority is not None:
        params["priority"] = priority
    return {"id": step_id, "action": "jackil_ticket", "params": params}


def _note_step(step_id="a", key="backup", body="signed body"):
    return {"id": step_id, "action": "jackil_note",
            "params": {"key": key, "body": body}}


def _resolve_step(step_id="a", key="backup", note=None, status=None):
    params = {"key": key}
    if note is not None:
        params["note"] = note
    if status is not None:
        params["status"] = status
    return {"id": step_id, "action": "jackil_resolve", "params": params}


def _run(host, steps, results=None, state=Task.State.COMPLETED):
    """Apply the Jackil steps of a task the result endpoint just stored.

    *results* maps a step id to the result dict the agent reported for it; a
    step missing from it is reported ``ok`` with no fields, so the signed text
    is what the server falls back to.
    """
    results = results or {}
    reports = [{"id": step["id"], "status": "ok", "result": results.get(step["id"], {})}
               for step in steps]
    task = Task.objects.create(
        host=host, action="_script", nonce=uuid.uuid4().hex,
        params={"steps": steps}, result_data={"steps": reports}, state=state)
    task_actions.apply(task)
    return task


class TicketTests(TestCase):
    def setUp(self):
        config.invalidate()
        _turn_on()
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32)

    def tearDown(self):
        config.invalidate()

    @mock.patch(_GET, return_value={"id": 42, "status": "in_progress"})
    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_ticket_opens_once_then_notes(self, create, note, status, get):
        step = _ticket_step()
        _run(self.host, [step])
        self.assertEqual(create.call_count, 1)
        link = JackilWorkTicket.objects.get()
        self.assertEqual(link.ticket_id, 42)
        self.assertEqual(link.key, "backup")
        self.assertEqual(link.url, "https://help.example.com/tickets/42/")
        self.assertIsNone(link.closed_at)
        note.assert_not_called()

        _run(self.host, [step])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(JackilWorkTicket.objects.count(), 1)
        self.assertIn("happened again", note.call_args[0][1])
        self.assertEqual(note.call_args[0][0], 42)

    @mock.patch(_CREATE, return_value={"id": 42})
    def test_ticket_body_names_the_host_key_and_task(self, create):
        task = _run(self.host, [_ticket_step(description="rsync exited 23")])
        body = create.call_args.kwargs["description"]
        self.assertIn("rsync exited 23", body)
        self.assertIn(f"Host: {self.host.hostname}", body)
        self.assertIn("Key: backup", body)
        self.assertIn(f"Opened by Vigil task {task.id}.", body)
        self.assertEqual(create.call_args.kwargs["title"], "web1: signed title")

    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_keys_are_per_host(self, create, note):
        other = Host.objects.create(hostname="web2", agent_token="u" * 32)
        step = _ticket_step()
        _run(self.host, [step])
        _run(other, [step])
        self.assertEqual(create.call_count, 2)
        self.assertEqual(JackilWorkTicket.objects.count(), 2)
        note.assert_not_called()

    @mock.patch(_CREATE, return_value={"id": 42})
    def test_priority_comes_from_the_signed_task(self, create):
        """The whole trust argument: the report's priority is decoration."""
        _run(self.host, [_ticket_step(priority="low")],
             {"a": {"priority": "critical"}})
        self.assertEqual(create.call_args.kwargs["priority"], "low")

    @mock.patch(_CREATE, return_value={"id": 42})
    def test_an_unknown_or_missing_priority_is_medium(self, create):
        _run(self.host, [_ticket_step(priority="urgent")])
        self.assertEqual(create.call_args.kwargs["priority"], "medium")

    @mock.patch(_GET, return_value={"id": 42, "status": "open"})
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_the_repeat_note_carries_the_description_too(self, create, note, get):
        step = _ticket_step(description="rsync exited 23")
        _run(self.host, [step])
        _run(self.host, [step])
        body = note.call_args[0][1]
        self.assertIn("happened again", body)
        self.assertIn("rsync exited 23", body)

    @mock.patch(_NOTE)
    @mock.patch(_CREATE, side_effect=[{"id": 42}, {"id": 43}])
    def test_a_ticket_closed_in_jackil_is_not_noted_again(self, create, note):
        """Someone resolved it by hand: the repeat opens a fresh ticket, and the
        old link becomes history instead of swallowing every repeat."""
        step = _ticket_step()
        _run(self.host, [step])
        for gone in ({"id": 42, "status": "resolved"}, client.JackilNotFound("404")):
            JackilWorkTicket.objects.all().delete()
            JackilWorkTicket.objects.create(host=self.host, key="backup", ticket_id=42)
            create.side_effect = [{"id": 43}]
            with mock.patch(_GET, side_effect=[gone]):
                _run(self.host, [step])
            note.assert_not_called()
            self.assertIsNotNone(JackilWorkTicket.objects.get(ticket_id=42).closed_at)
            self.assertIsNone(JackilWorkTicket.objects.get(ticket_id=43).closed_at)

    @mock.patch(_GET, side_effect=client.JackilError("Jackil is down"))
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_an_outage_never_opens_a_duplicate(self, create, note, get):
        step = _ticket_step()
        _run(self.host, [step])
        _run(self.host, [step])
        self.assertEqual(create.call_count, 1)
        self.assertIsNone(JackilWorkTicket.objects.get().closed_at)

    @mock.patch(_CREATE, return_value={"id": 42})
    def test_the_title_is_the_reported_one(self, create):
        _run(self.host, [_ticket_step(title="signed title")],
             {"a": {"title": "rendered title"}})
        self.assertEqual(create.call_args.kwargs["title"], "web1: rendered title")

    @mock.patch(_CREATE, return_value={"id": 42})
    def test_a_keyless_step_opens_nothing(self, create):
        _run(self.host, [_ticket_step(key="   ")])
        create.assert_not_called()
        self.assertEqual(JackilWorkTicket.objects.count(), 0)

    @mock.patch(_CREATE, side_effect=client.JackilError("connection refused"))
    def test_a_jackil_outage_does_not_raise_or_store(self, create):
        _run(self.host, [_ticket_step()])
        create.assert_called_once()
        self.assertEqual(JackilWorkTicket.objects.count(), 0)


class NoteTests(TestCase):
    def setUp(self):
        config.invalidate()
        _turn_on()
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32)

    def tearDown(self):
        config.invalidate()

    @mock.patch(_NOTE)
    def test_note_goes_to_the_open_ticket(self, note):
        JackilWorkTicket.objects.create(host=self.host, key="backup", ticket_id=42)
        _run(self.host, [_note_step()], {"a": {"body": "retried, still failing"}})
        self.assertEqual(note.call_args[0], (42, "retried, still failing"))

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE)
    def test_note_without_open_ticket_does_nothing(self, create, note, status):
        JackilWorkTicket.objects.create(host=self.host, key="backup", ticket_id=42,
                                        closed_at=now() - timedelta(seconds=1))
        _run(self.host, [_note_step()])
        create.assert_not_called()
        note.assert_not_called()
        status.assert_not_called()


class ResolveTests(TestCase):
    def setUp(self):
        config.invalidate()
        _turn_on()
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32)

    def tearDown(self):
        config.invalidate()

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 99})
    def test_resolve_closes_the_link_and_a_new_failure_reopens(self, create, note,
                                                               status):
        _run(self.host, [_ticket_step()])
        link = JackilWorkTicket.objects.get()
        _run(self.host, [_resolve_step(note="backup ran clean")])
        status.assert_called_once_with(link.ticket_id, "resolved")
        self.assertIn("backup ran clean", note.call_args[0][1])
        link.refresh_from_db()
        self.assertIsNotNone(link.closed_at)

        _run(self.host, [_ticket_step()])
        self.assertEqual(create.call_count, 2)
        self.assertEqual(JackilWorkTicket.objects.filter(closed_at__isnull=True).count(), 1)

    @mock.patch(_STATUS, side_effect=client.JackilError("Jackil is down"))
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_resolve_keeps_link_open_when_jackil_fails(self, create, note, status):
        _run(self.host, [_ticket_step()])
        _run(self.host, [_resolve_step()])
        status.assert_called_once()
        self.assertIsNone(JackilWorkTicket.objects.get().closed_at)

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE, return_value={"id": 42})
    def test_a_signed_closed_status_is_honoured(self, create, note, status):
        _run(self.host, [_ticket_step()])
        ticket_id = JackilWorkTicket.objects.get().ticket_id
        _run(self.host, [_resolve_step(status="closed")], {"a": {"status": "resolved"}})
        status.assert_called_once_with(ticket_id, "closed")

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    def test_resolve_without_open_ticket_does_nothing(self, note, status):
        _run(self.host, [_resolve_step()])
        note.assert_not_called()
        status.assert_not_called()


class DisabledTests(TestCase):
    def setUp(self):
        config.invalidate()
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32)

    def tearDown(self):
        config.invalidate()

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE)
    def test_disabled_jackil_does_nothing(self, create, note, status):
        create.return_value = {"id": 42}
        _run(self.host, [_ticket_step(), _note_step(), _resolve_step()])
        create.assert_not_called()
        note.assert_not_called()
        status.assert_not_called()
        self.assertEqual(JackilWorkTicket.objects.count(), 0)

    @mock.patch(_STATUS)
    @mock.patch(_NOTE)
    @mock.patch(_CREATE)
    def test_a_task_with_no_jackil_step_never_touches_jackil(self, create, note,
                                                             status):
        """With Jackil switched off, an unrelated task must not log a warning
        either — the empty-step check comes first."""
        task = Task.objects.create(
            host=self.host, action="_script", nonce=uuid.uuid4().hex,
            params={"steps": [{"id": "a", "action": "check_service",
                               "params": {"service_name": "nginx"}}]},
            result_data={"steps": [{"id": "a", "status": "ok", "result": {}}]},
            state=Task.State.COMPLETED)
        with mock.patch(_ENABLED, side_effect=AssertionError("enabled() was called")):
            task_actions.apply(task)
