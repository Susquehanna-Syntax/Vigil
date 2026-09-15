"""Tests for the Jackil integration.

Nothing here talks to a real Jackil — the client is patched at the boundary.
What is worth proving is the behaviour around the call: that a re-fire does
not open a second ticket, that a helpdesk outage cannot cost an alert, and
that the severity floor is honoured.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.instance import config
from apps.jackil import client, service
from apps.jackil.models import JackilTicket


def _turn_on(min_severity="info"):
    config.write("JACKIL_ENABLED", True)
    config.write("JACKIL_URL", "https://help.example.com")
    config.write("JACKIL_API_KEY", "key-123")
    config.write("JACKIL_MIN_SEVERITY", min_severity)


class JackilTestCase(TestCase):
    def setUp(self):
        config.invalidate()
        self.host = Host.objects.create(
            hostname="web1", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )
        self.rule = AlertRule.objects.create(
            name="Disk almost full", category="disk", metric="disk_percent",
            operator="gt", threshold=95.0,
            severity=AlertRule.Severity.CRITICAL,
        )

    def tearDown(self):
        config.invalidate()

    def _alert(self, severity="critical"):
        return Alert.objects.create(
            host=self.host, rule=self.rule, severity=severity,
            message="/ is at 97%", metric_value=97.0,
        )


class TicketCreationTests(JackilTestCase):
    def test_nothing_happens_until_it_is_switched_on(self):
        """URL and key present but the toggle off must stay silent — someone
        pasting credentials is not the same as someone opting in."""
        config.write("JACKIL_URL", "https://help.example.com")
        config.write("JACKIL_API_KEY", "key-123")
        with mock.patch.object(client, "create_ticket") as create:
            service.on_alert_sent(self._alert())
        create.assert_not_called()
        self.assertEqual(JackilTicket.objects.count(), 0)

    def test_fired_alert_opens_a_ticket(self):
        _turn_on()
        with mock.patch.object(client, "create_ticket",
                               return_value={"id": 42}) as create:
            link = service.on_alert_sent(self._alert())
        self.assertEqual(link.ticket_id, 42)
        self.assertEqual(link.url, "https://help.example.com/tickets/42/")
        body = create.call_args.kwargs
        self.assertIn("web1", body["title"])
        self.assertEqual(body["priority"], "critical")
        self.assertIn("/ is at 97%", body["description"])

    def test_severity_maps_to_priority(self):
        _turn_on()
        for severity, priority in (("critical", "critical"), ("warning", "high"),
                                   ("info", "low")):
            with mock.patch.object(client, "create_ticket",
                                   return_value={"id": 1}) as create:
                service.on_alert_sent(self._alert(severity))
            self.assertEqual(create.call_args.kwargs["priority"], priority)
            JackilTicket.objects.all().delete()

    def test_severity_floor_is_honoured(self):
        """The default is critical-only: a ticket per info alert turns the
        helpdesk queue into a metrics feed."""
        _turn_on(min_severity="critical")
        with mock.patch.object(client, "create_ticket") as create:
            service.on_alert_sent(self._alert("warning"))
            service.on_alert_sent(self._alert("info"))
        create.assert_not_called()
        with mock.patch.object(client, "create_ticket", return_value={"id": 7}):
            self.assertIsNotNone(service.on_alert_sent(self._alert("critical")))

    def test_refiring_does_not_open_a_second_ticket(self):
        """The whole reason this is not a webhook: six re-fires of one bad
        disk must not leave six tickets."""
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket",
                               return_value={"id": 42}) as create:
            for _ in range(4):
                service.on_alert_sent(alert)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(JackilTicket.objects.filter(alert=alert).count(), 1)

    def test_tags_and_requester_are_passed_through(self):
        _turn_on()
        config.write("JACKIL_TAGS", "vigil,noc")
        config.write("JACKIL_REQUESTER_EMAIL", "noc@example.com")
        with mock.patch.object(client, "create_ticket",
                               return_value={"id": 9}) as create:
            service.on_alert_sent(self._alert())
        self.assertEqual(create.call_args.kwargs["tags"], "vigil,noc")
        self.assertEqual(create.call_args.kwargs["requester_email"], "noc@example.com")

    def test_a_jackil_outage_does_not_raise(self):
        """Monitoring must survive the helpdesk being down."""
        _turn_on()
        with mock.patch.object(client, "create_ticket",
                               side_effect=client.JackilError("connection refused")):
            self.assertIsNone(service.on_alert_sent(self._alert()))
        self.assertEqual(JackilTicket.objects.count(), 0)

    def test_a_response_without_an_id_is_not_stored(self):
        _turn_on()
        with mock.patch.object(client, "create_ticket", return_value={"detail": "ok"}):
            self.assertIsNone(service.on_alert_sent(self._alert()))
        self.assertEqual(JackilTicket.objects.count(), 0)


class TicketResolutionTests(JackilTestCase):
    def test_clearing_notes_and_resolves(self):
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        alert.resolved_at = now()
        alert.save(update_fields=["resolved_at"])
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status") as status:
            service.on_alert_resolved(alert)
        note.assert_called_once()
        self.assertIn("cleared", note.call_args.args[1])
        status.assert_called_once_with(42, "resolved")

    def test_resolve_on_clear_can_be_turned_off(self):
        _turn_on()
        config.write("JACKIL_RESOLVE_ON_CLEAR", False)
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        with mock.patch.object(client, "add_note"), \
                mock.patch.object(client, "set_status") as status:
            service.on_alert_resolved(alert)
        status.assert_not_called()

    def test_a_flapping_alert_notes_the_clear_once(self):
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status"):
            service.on_alert_resolved(alert)
            service.on_alert_resolved(alert)
            service.on_alert_resolved(alert)
        self.assertEqual(note.call_count, 1)

    def test_clearing_an_alert_with_no_ticket_is_a_no_op(self):
        _turn_on()
        with mock.patch.object(client, "add_note") as note:
            service.on_alert_resolved(self._alert())
        note.assert_not_called()

    def test_a_failed_update_leaves_the_link_uncleared(self):
        """So the next clear can try again rather than the note being lost."""
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            link = service.on_alert_sent(alert)
        with mock.patch.object(client, "add_note",
                               side_effect=client.JackilError("502")):
            service.on_alert_resolved(alert)
        link.refresh_from_db()
        self.assertIsNone(link.cleared_at)


class HookWiringTests(JackilTestCase):
    """The event bus had two subscribers and no emitter. These prove the
    alert lifecycle now actually reaches them."""

    def setUp(self):
        super().setUp()
        from apps.jackil.apps import wire
        wire()

    def test_dispatch_emits_fired_and_opens_a_ticket(self):
        from apps.alerts.notifications import dispatch_alert_notification

        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            dispatch_alert_notification(alert, event="firing")
        self.assertTrue(JackilTicket.objects.filter(alert=alert).exists())

    def test_dispatch_emits_resolved(self):
        from apps.alerts.notifications import dispatch_alert_notification

        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status"):
            dispatch_alert_notification(alert, event="resolved")
        note.assert_called_once()

    def test_a_raising_subscriber_cannot_break_dispatch(self):
        from apps.alerts.notifications import dispatch_alert_notification

        _turn_on()
        with mock.patch.object(service, "on_alert_sent",
                               side_effect=RuntimeError("boom")):
            dispatch_alert_notification(self._alert(), event="firing")


class ClientTests(JackilTestCase):
    def test_ping_needs_configuration(self):
        with self.assertRaises(client.JackilError):
            client.ping()

    def test_a_403_names_the_actual_problem(self):
        """Jackil's IsStaff refuses a customer-role key. "403" alone sends
        someone hunting the wrong thing."""
        _turn_on()
        response = mock.Mock(status_code=403, content=b"", text="")
        with mock.patch("apps.jackil.client.requests.request", return_value=response):
            with self.assertRaises(client.JackilError) as caught:
                client.ping()
        self.assertIn("agent or admin", str(caught.exception))

    def test_html_answer_is_reported_as_wrong_url(self):
        _turn_on()
        response = mock.Mock(status_code=200, content=b"<html>", text="<html>")
        response.json.side_effect = ValueError
        with mock.patch("apps.jackil.client.requests.request", return_value=response):
            with self.assertRaises(client.JackilError) as caught:
                client.ping()
        self.assertIn("is that URL Jackil", str(caught.exception))


class ApiTests(JackilTestCase):
    def setUp(self):
        super().setUp()
        self.client_api = APIClient()
        user = get_user_model().objects.create_user("jadmin", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client_api.force_authenticate(user)

    def test_recent_tickets_lists_what_was_opened(self):
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        body = self.client_api.get("/api/v1/jackil/tickets/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["tickets"][0]["ticket_id"], 42)
        self.assertEqual(body["tickets"][0]["host"], "web1")

    def test_viewer_cannot_list_tickets(self):
        viewer = get_user_model().objects.create_user("jviewer", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        api = APIClient()
        api.force_authenticate(viewer)
        self.assertEqual(api.get("/api/v1/jackil/tickets/").status_code, 403)

    def test_connection_test_reports_a_failure(self):
        _turn_on()
        with mock.patch.object(client, "ping",
                               side_effect=client.JackilError("nope")):
            body = self.client_api.post("/api/v1/instance-settings/test/",
                                        {"target": "jackil"}, format="json").json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["detail"], "nope")

    def test_connection_test_says_when_tickets_are_still_off(self):
        config.write("JACKIL_URL", "https://help.example.com")
        config.write("JACKIL_API_KEY", "key-123")
        with mock.patch.object(client, "ping", return_value={}):
            body = self.client_api.post("/api/v1/instance-settings/test/",
                                        {"target": "jackil"}, format="json").json()
        self.assertTrue(body["ok"])
        self.assertIn("still off", body["detail"])

    def test_the_api_key_is_never_sent_back(self):
        config.write("JACKIL_API_KEY", "key-123")
        body = self.client_api.get("/api/v1/instance-settings/").json()
        self.assertNotIn("key-123", str(body))
