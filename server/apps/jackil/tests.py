"""Tests for the Jackil integration.

Nothing here talks to a real Jackil — the client is patched at the boundary.
What is worth proving is the behaviour around the call: that a re-fire does
not open a second ticket, that a helpdesk outage cannot cost an alert, and
that the severity floor is honoured.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.instance import config
from apps.jackil import client, service
from apps.jackil.api_schema import (
    JackilErrorSerializer,
    JackilKeyListSerializer,
    JackilKeySerializer,
    JackilMintedKeySerializer,
    JackilRelatedSerializer,
    JackilRelatedUnavailableSerializer,
    JackilRelatedUnconfiguredSerializer,
    JackilRemoteWidgetSerializer,
    JackilTicketsSerializer,
)
from apps.jackil.models import JackilTicket
from vigil.schema_check import assert_shape

from .test_integration import KEYS, _admin


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


class ApiSchemaTests(JackilTestCase):
    """Phase 50: the session endpoints a helpdesk's settings pane calls answer
    with what ``apps/jackil/api_schema.py`` documents. Data is present
    everywhere — a list with no row in it checks nothing — and each of the
    answers ``related`` and a Jackil tile can give is checked, not only the
    happy one."""

    def setUp(self):
        super().setUp()
        self.client_api = APIClient()
        user = get_user_model().objects.create_user("jadmin", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client_api.force_authenticate(user)

    @staticmethod
    def _helpdesk(payload):
        """The ``requests`` response a configured Jackil would hand back."""
        body = json.dumps(payload).encode()
        return mock.Mock(status_code=200, content=body, text="",
                         json=lambda: payload)

    def test_50_responses_match_the_api_schema(self):
        # The ticket list the settings pane draws: one row, because an empty
        # list would check no row shape at all.
        _turn_on()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(self._alert())
        tickets = self.client_api.get("/api/v1/jackil/tickets/")
        self.assertEqual(tickets.status_code, 200, tickets.content)
        self.assertTrue(tickets.json()["tickets"], "an empty list checks no row shape")
        assert_shape(self, tickets.json(), JackilTicketsSerializer)

        # The key admin's three answers: minted once, then listed, then
        # revoked. These are settings-page endpoints and the settings page is
        # hidden from a site admin (SETTINGS_ACCESS), so this caller is the
        # instance admin the pane belongs to.
        keys = self.client_class()
        keys.force_login(_admin("boss"))
        minted = keys.post(KEYS, {"name": "Front desk"}, format="json")
        self.assertEqual(minted.status_code, 201, minted.content)
        assert_shape(self, minted.json(), JackilMintedKeySerializer)
        pk = minted.json()["id"]

        # ``enabled`` has to arrive as the JSON boolean the view reads: a form
        # encoding would send the word, not the value.
        enabled = keys.post(f"{KEYS}{pk}/devices/", json.dumps({"enabled": True}),
                            content_type="application/json")
        self.assertEqual(enabled.status_code, 200, enabled.content)
        assert_shape(self, enabled.json(), JackilKeySerializer)

        listed = keys.get(KEYS)
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertTrue(listed.json()["keys"], "an empty list checks no row shape")
        assert_shape(self, listed.json(), JackilKeyListSerializer)

        revoked = keys.post(f"{KEYS}{pk}/revoke/", {}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.content)
        assert_shape(self, revoked.json(), JackilKeySerializer)
        missing = keys.post(f"{KEYS}999999/revoke/", {}, format="json")
        self.assertEqual(missing.status_code, 404, missing.content)
        assert_shape(self, missing.json(), JackilErrorSerializer)

        # ``related``: the link list, then each reason the drawer stays empty.
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        return_value=self._helpdesk(
                            {"links": [{"relation": "fixes", "type": "ticket",
                                        "id": "7", "label": "#7 Printer on fire",
                                        "url": "https://jackil.example/tickets/7/",
                                        "status": "Open", "tone": "info"}],
                             "items": [{"id": "3", "name": "Edge switch",
                                        "url": "https://jackil.example/items/3/"}]})):
            linked = self.client_api.get("/api/v1/jackil/related/",
                                         {"type": "host", "id": str(self.host.pk)})
        self.assertEqual(linked.status_code, 200, linked.content)
        self.assertTrue(linked.json()["links"], "an empty list checks no row shape")
        assert_shape(self, linked.json(), JackilRelatedSerializer)

        config.write("JACKIL_ENABLED", False)
        unconfigured = self.client_api.get("/api/v1/jackil/related/",
                                           {"type": "host", "id": str(self.host.pk)})
        self.assertEqual(unconfigured.status_code, 200, unconfigured.content)
        assert_shape(self, unconfigured.json(), JackilRelatedUnconfiguredSerializer)

        _turn_on()
        with mock.patch.object(client, "related",
                               side_effect=client.JackilError(
                                   "Jackil is not answering right now.")):
            down = self.client_api.get("/api/v1/jackil/related/",
                                       {"type": "host", "id": str(self.host.pk)})
        self.assertEqual(down.status_code, 200, down.content)
        assert_shape(self, down.json(), JackilRelatedUnavailableSerializer)

        # A Jackil tile on a Vigil dashboard: the helpdesk's envelope, and the
        # sentence that replaces it when there is no helpdesk to ask. The
        # catalogue is cached for five minutes and the "stop calling" flag
        # lasts thirty seconds, so both start from an empty cache.
        cache.clear()
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        side_effect=[self._helpdesk(
                            {"version": 1,
                             "widgets": [{"kind": "stat_tile", "title": "Stat tile",
                                          "description": "One number.",
                                          "shape": "stat", "settings": {}}]}),
                            self._helpdesk({"version": 1, "shape": "stat",
                                            "data": {"value": 7,
                                                     "label": "Open tickets"}})]):
            tile = self.client_api.get("/api/v1/jackil/widgets/jackil:stat_tile/")
        self.assertEqual(tile.status_code, 200, tile.content)
        assert_shape(self, tile.json(), JackilRemoteWidgetSerializer)

        config.write("JACKIL_ENABLED", False)
        off = self.client_api.get("/api/v1/jackil/widgets/jackil:stat_tile/")
        self.assertEqual(off.status_code, 200, off.content)
        assert_shape(self, off.json(), JackilRemoteWidgetSerializer)

        # A kind the registry does not know is refused before anyone is asked.
        gone = self.client_api.get("/api/v1/jackil/widgets/not-a-jackil-tile/")
        self.assertEqual(gone.status_code, 404, gone.content)
        assert_shape(self, gone.json(), JackilErrorSerializer)


class RefireTests(JackilTestCase):
    """A cleared alert that comes back says so on its ticket, once an hour."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def _ticketed_alert(self):
        _turn_on()
        alert = self._alert()
        with mock.patch.object(client, "create_ticket", return_value={"id": 42}):
            service.on_alert_sent(alert)
        return alert

    def test_refire_after_a_clear_notes_and_reopens(self):
        alert = self._ticketed_alert()
        with mock.patch.object(client, "add_note"), \
                mock.patch.object(client, "set_status"):
            service.on_alert_resolved(alert)
        alert.flap_count = 2
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status") as status:
            service.on_alert_refired(alert)
        note.assert_called_once()
        self.assertIn("fired again", note.call_args.args[1])
        self.assertIn("2 times", note.call_args.args[1])
        status.assert_called_once_with(42, "open")
        self.assertIsNone(JackilTicket.objects.get(alert=alert).cleared_at)

    def test_refire_notes_at_most_once_an_hour(self):
        alert = self._ticketed_alert()
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status"):
            service.on_alert_refired(alert)
            service.on_alert_refired(alert)
            service.on_alert_refired(alert)
        self.assertEqual(note.call_count, 1)

    def test_refire_with_no_ticket_is_a_no_op(self):
        _turn_on()
        with mock.patch.object(client, "add_note") as note:
            service.on_alert_refired(self._alert())
        note.assert_not_called()

    def test_refire_hook_is_wired(self):
        from vigil import hooks
        alert = self._ticketed_alert()
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status"):
            hooks.emit("alert_refired", alert=alert)
        note.assert_called_once()

    def test_a_throttled_refire_still_reopens_and_rearms_the_clear(self):
        alert = self._ticketed_alert()
        with mock.patch.object(client, "add_note"), \
                mock.patch.object(client, "set_status"):
            service.on_alert_resolved(alert)
            service.on_alert_refired(alert)       # noted, reopened
            service.on_alert_resolved(alert)      # cleared again
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status") as status:
            service.on_alert_refired(alert)       # inside the hour
        note.assert_not_called()
        status.assert_called_once_with(42, "open")
        self.assertIsNone(JackilTicket.objects.get(alert=alert).cleared_at)

    def test_clears_inside_a_flap_change_state_without_a_note(self):
        alert = self._ticketed_alert()
        with mock.patch.object(client, "add_note"), \
                mock.patch.object(client, "set_status"):
            service.on_alert_resolved(alert)
            service.on_alert_refired(alert)
        with mock.patch.object(client, "add_note") as note, \
                mock.patch.object(client, "set_status") as status:
            service.on_alert_resolved(alert)
        note.assert_not_called()
        status.assert_called_once_with(42, "resolved")
