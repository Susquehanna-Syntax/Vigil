"""Device alerts (S9 phase 04b): two failed results open one, a pass resolves it.

The alert is rule-less and device-bound, so these tests drive the evaluator
directly with ORM rows (as ``test_outdated_agents.py`` does for its rule-less
alert), plus the API the Alerts page talks to and the check-write paths that
quiet an alert when nothing polls the device any more.
"""

import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.device_alerts import evaluate, resolve_for_check
from apps.alerts.models import DeviceAlert, NotificationChannel
from apps.alerts.tasks import prune_old_alerts
from apps.hosts.device_checks import store_check_results
from apps.hosts.models import CheckResult, DeviceCheck, Host, UnmanagedDevice
from vigil.css_text import design_css

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css"
TEMPLATES = ROOT / "templates"


def make_device(name="sw-01"):
    return UnmanagedDevice.objects.create(name=name, device_type="switch")


def make_check(device, kind=DeviceCheck.Kind.ICMP, params=None, enabled=True):
    return DeviceCheck.objects.create(device=device, kind=kind, enabled=enabled,
                                      params=params if params is not None
                                      else {"host": "10.0.0.1"})


def add_result(check, ok, seconds_ago=0, detail=""):
    return CheckResult.objects.create(
        device_check=check, ok=ok, detail=detail,
        time=timezone.now() - timedelta(seconds=seconds_ago))


class TwoFailuresFireTests(TestCase):
    def setUp(self):
        self.device = make_device("core-switch")
        self.check = make_check(self.device)

    def _fire_twice(self):
        add_result(self.check, False, seconds_ago=20, detail="100% packet loss")
        add_result(self.check, False, seconds_ago=10, detail="100% packet loss")

    def test_one_failed_result_opens_nothing(self):
        add_result(self.check, False, detail="100% packet loss")
        self.assertEqual(evaluate({self.check.id}), (0, 0))
        self.assertEqual(DeviceAlert.objects.count(), 0)

    def test_two_failures_in_a_row_open_one_firing_alert_naming_the_device(self):
        self._fire_twice()
        self.assertEqual(evaluate({self.check.id}), (1, 0))
        alert = DeviceAlert.objects.get()
        self.assertEqual(alert.state, DeviceAlert.State.FIRING)
        self.assertEqual(alert.device, self.device)
        self.assertEqual(alert.device_check, self.check)
        self.assertEqual(alert.severity, "critical")
        self.assertIn("core-switch", alert.message)
        self.assertIn("100% packet loss", alert.message)

    def test_a_third_failure_does_not_open_a_second_alert(self):
        self._fire_twice()
        evaluate({self.check.id})
        add_result(self.check, False, detail="still down")
        self.assertEqual(evaluate({self.check.id}), (0, 0))
        self.assertEqual(DeviceAlert.objects.count(), 1)

    def test_a_long_detail_is_cut_to_the_limit(self):
        # A detail is at most 300 characters (CheckResult.detail, which
        # Postgres enforces), so the cut only bites when the target is long
        # too: a 260-character URL plus a full detail is past 500.
        url = "https://wiki.lan/" + "a" * 243
        check = make_check(self.device, kind=DeviceCheck.Kind.HTTP, params={"url": url})
        add_result(check, False, detail="x" * 300)
        add_result(check, False, detail="y" * 300)
        evaluate({check.id})
        message = DeviceAlert.objects.get().message
        self.assertEqual(len(message), 500)
        # The cut takes a bite out of the poller's detail, never out of the
        # part that says which device and which check.
        self.assertTrue(message.startswith(f"core-switch is down: HTTP {url} failed twice: y"))

    def test_the_target_is_labelled_the_way_the_ui_labels_a_check(self):
        for kind, params, expected in (
            (DeviceCheck.Kind.ICMP, {"host": "10.0.0.1"}, "Ping 10.0.0.1"),
            (DeviceCheck.Kind.TCP, {"host": "db.lan", "port": 5432}, "TCP db.lan:5432"),
            (DeviceCheck.Kind.HTTP, {"url": "https://wiki.lan/"}, "HTTP https://wiki.lan/"),
            (DeviceCheck.Kind.DNS, {"name": "example.com"}, "DNS example.com"),
        ):
            with self.subTest(kind=kind):
                check = make_check(make_device("edge"), kind=kind, params=params)
                add_result(check, False)
                add_result(check, False)
                evaluate({check.id})
                self.assertIn(expected, DeviceAlert.objects.get(device_check=check).message)

    def test_a_disabled_check_fires_nothing(self):
        check = make_check(make_device("quiet"), enabled=False)
        add_result(check, False)
        add_result(check, False)
        self.assertEqual(evaluate({check.id}), (0, 0))
        self.assertEqual(DeviceAlert.objects.count(), 0)

    def test_results_of_other_checks_are_left_alone(self):
        other = make_check(make_device("other-switch"))
        add_result(other, False, detail="down")
        add_result(other, False, detail="down")
        self._fire_twice()
        self.assertEqual(evaluate({self.check.id}), (1, 0))
        self.assertEqual(DeviceAlert.objects.count(), 1)


class ResolutionTests(TestCase):
    def setUp(self):
        self.device = make_device()
        self.check = make_check(self.device)

    def _fire(self):
        add_result(self.check, False, seconds_ago=20)
        add_result(self.check, False, seconds_ago=10)
        evaluate({self.check.id})

    def test_a_passing_result_resolves_the_alert(self):
        self._fire()
        add_result(self.check, True, detail="ok")
        self.assertEqual(evaluate({self.check.id}), (0, 1))
        alert = DeviceAlert.objects.get()
        self.assertEqual(alert.state, DeviceAlert.State.RESOLVED)
        self.assertIsNotNone(alert.resolved_at)

    def test_an_acknowledged_alert_resolves_too(self):
        self._fire()
        alert = DeviceAlert.objects.get()
        alert.state = DeviceAlert.State.ACKNOWLEDGED
        alert.save(update_fields=["state"])
        add_result(self.check, True)
        self.assertEqual(evaluate({self.check.id}), (0, 1))
        self.assertEqual(DeviceAlert.objects.get().state, DeviceAlert.State.RESOLVED)

    def test_ok_then_fail_does_not_fire_because_two_are_needed(self):
        add_result(self.check, False, seconds_ago=20)
        add_result(self.check, True, seconds_ago=10)
        add_result(self.check, False)
        self.assertEqual(evaluate({self.check.id}), (0, 0))
        self.assertEqual(DeviceAlert.objects.count(), 0)

    def test_a_resolved_alert_can_fire_again_after_two_new_failures(self):
        self._fire()
        add_result(self.check, True)
        evaluate({self.check.id})
        add_result(self.check, False)
        add_result(self.check, False)
        self.assertEqual(evaluate({self.check.id}), (1, 0))
        self.assertEqual(DeviceAlert.objects.filter(state=DeviceAlert.State.FIRING).count(), 1)


class CheckWriteQuietensTests(TestCase):
    """Disabling or deleting a check must not leave an alert saying "down"."""

    def setUp(self):
        self.client = APIClient()
        admin = get_user_model().objects.create_user("root")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_authenticate(admin)
        self.device = make_device()
        self.check = make_check(self.device)
        add_result(self.check, False, seconds_ago=20)
        add_result(self.check, False, seconds_ago=10)
        evaluate({self.check.id})

    def _url(self):
        return f"/api/v1/hosts/devices/{self.device.id}/checks/{self.check.id}/"

    def test_disabling_resolves_the_open_alert(self):
        resp = self.client.patch(self._url(), {"enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(DeviceAlert.objects.get().state, DeviceAlert.State.RESOLVED)

    def test_deleting_resolves_the_open_alert_first(self):
        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 204)
        alert = DeviceAlert.objects.get()
        self.assertEqual(alert.state, DeviceAlert.State.RESOLVED)
        # The FK is SET_NULL: the row survives its check, resolved.
        self.assertIsNone(alert.device_check_id)

    def test_resolve_for_check_notifies_nobody(self):
        with patch("apps.alerts.device_alerts.dispatch_device_alert") as sent:
            resolve_for_check(self.check)
        sent.assert_not_called()
        self.assertEqual(DeviceAlert.objects.get().state, DeviceAlert.State.RESOLVED)


class NotificationTests(TestCase):
    def setUp(self):
        self.device = make_device("edge-fw")
        self.check = make_check(self.device)
        add_result(self.check, False, seconds_ago=20, detail="no route to host")
        add_result(self.check, False, seconds_ago=10, detail="no route to host")

    def _payloads(self):
        with patch("apps.alerts.notifications.deliver",
                   side_effect=lambda channel, message: payloads.append(message.payload)):
            payloads = []
            evaluate({self.check.id})          # firing
            add_result(self.check, True)
            evaluate({self.check.id})          # resolved
        return payloads

    def test_firing_and_resolved_each_send_one_payload_of_the_host_shape(self):
        NotificationChannel.objects.create(
            name="ops", kind=NotificationChannel.Kind.WEBHOOK,
            config={"url": "https://hooks.example.com/ops"})
        payloads = self._payloads()
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertEqual(payload["host"], "edge-fw")
            self.assertIsNone(payload["host_id"])
            self.assertEqual(payload["device"], "edge-fw")
            self.assertEqual(payload["device_id"], str(self.device.id))
            self.assertIsNone(payload["rule"])
            self.assertIsNone(payload["metric_value"])
        self.assertEqual([p["event"] for p in payloads], ["firing", "resolved"])

    def test_a_channel_that_did_not_ask_for_the_event_is_skipped(self):
        NotificationChannel.objects.create(
            name="firing-only", kind=NotificationChannel.Kind.WEBHOOK, on_resolved=False,
            config={"url": "https://hooks.example.com/ops"})
        with patch("apps.alerts.notifications.deliver",
                   side_effect=lambda channel, message: seen.append(message.payload)):
            seen = []
            evaluate({self.check.id})
            add_result(self.check, True)
            evaluate({self.check.id})
        self.assertEqual([p["event"] for p in seen], ["firing"])


class StoreCheckResultsTests(TestCase):
    """The poller's check-in is what actually fires an alert (S9 02 + 04b)."""

    def setUp(self):
        self.poller = Host.objects.create(
            hostname="p1", agent_token="tok-p1-" + "h" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR, agent_poller=True)
        self.device = make_device()
        self.check = make_check(self.device, )
        self.check.poller = self.poller
        self.check.save(update_fields=["poller"])

    def _row(self, ok, seconds_ago=0):
        return {"check": str(self.check.id), "ok": ok, "latency_ms": 4.0,
                "detail": "" if ok else "100% packet loss",
                "at": (timezone.now() - timedelta(seconds=seconds_ago)).isoformat()}

    def test_two_failing_results_through_the_check_in_fire_an_alert(self):
        """The check-in queues the evaluation after commit; the worker fires."""
        with patch("apps.alerts.tasks.evaluate_device_alerts.delay",
                   side_effect=lambda ids: evaluate(ids)) as delay, \
                self.captureOnCommitCallbacks(execute=True):
            stored = store_check_results(self.poller, [self._row(False, 20), self._row(False, 10)])
        self.assertEqual(stored, 2)
        delay.assert_called_once_with([str(self.check.id)])
        alert = DeviceAlert.objects.get()
        self.assertEqual(alert.state, DeviceAlert.State.FIRING)
        self.assertIn("100% packet loss", alert.message)

    def test_nothing_is_evaluated_inside_the_check_in(self):
        """A webhook can take 10 seconds; the check-in only queues."""
        with patch("apps.alerts.tasks.evaluate_device_alerts.delay"), \
                self.captureOnCommitCallbacks(execute=False) as callbacks:
            store_check_results(self.poller, [self._row(False, 20), self._row(False, 10)])
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(DeviceAlert.objects.count(), 0)

    def test_a_broker_that_refuses_the_task_still_stores_the_results(self):
        with patch("apps.alerts.tasks.evaluate_device_alerts.delay",
                   side_effect=ConnectionError("broker down")), \
                self.assertLogs("apps.alerts.device_alerts", level="ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            stored = store_check_results(self.poller, [self._row(False), self._row(False)])
        self.assertEqual(stored, 2)
        self.assertEqual(CheckResult.objects.count(), 2)
        self.assertEqual(DeviceAlert.objects.count(), 0)

    def test_the_task_reports_what_it_did(self):
        from apps.alerts.tasks import evaluate_device_alerts
        add_result(self.check, False, seconds_ago=20)
        add_result(self.check, False, seconds_ago=10)
        self.assertEqual(evaluate_device_alerts([str(self.check.id)]),
                         "Device alerts: 1 fired, 0 resolved")


class DeviceAlertApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_authenticate(self.user)
        self.device = make_device("core-switch")
        self.check = make_check(self.device)

    def _alert(self):
        add_result(self.check, False, seconds_ago=20, detail="100% packet loss")
        add_result(self.check, False, seconds_ago=10, detail="100% packet loss")
        evaluate({self.check.id})
        return DeviceAlert.objects.get()

    def test_open_list_carries_the_device_and_the_message(self):
        alert = self._alert()
        resp = self.client.get("/api/v1/alerts/devices/")
        self.assertEqual(resp.status_code, 200, resp.content)
        row = resp.json()[0]
        self.assertEqual(row["id"], str(alert.id))
        self.assertEqual(row["device_id"], str(self.device.id))
        self.assertEqual(row["device_name"], "core-switch")
        self.assertEqual(row["check_id"], str(self.check.id))
        self.assertEqual(row["state"], "firing")
        self.assertEqual(row["severity"], "critical")
        self.assertIn("100% packet loss", row["message"])
        self.assertIsNotNone(row["fired_at"])
        self.assertIsNone(row["acknowledged_at"])
        self.assertIsNone(row["resolved_at"])

    def test_resolved_rows_are_hidden_until_state_all(self):
        alert = self._alert()
        add_result(self.check, True)
        evaluate({self.check.id})
        self.assertEqual(self.client.get("/api/v1/alerts/devices/").json(), [])
        body = self.client.get("/api/v1/alerts/devices/?state=all").json()
        self.assertEqual([row["state"] for row in body], ["resolved"])
        self.assertEqual(
            [row["id"] for row in self.client.get("/api/v1/alerts/devices/?state=resolved").json()],
            [str(alert.id)])

    def test_the_list_is_newest_first_and_capped(self):
        devices = [make_device(f"d{i}") for i in range(3)]
        for device in devices:
            check = make_check(device)
            add_result(check, False, seconds_ago=20)
            add_result(check, False, seconds_ago=10)
            evaluate({check.id})
        DeviceAlert.objects.update(fired_at=timezone.now() - timedelta(hours=1))
        newest = DeviceAlert.objects.first()
        newest.fired_at = timezone.now()
        newest.save(update_fields=["fired_at"])
        body = self.client.get("/api/v1/alerts/devices/").json()
        self.assertEqual(body[0]["id"], str(newest.id))
        self.assertLessEqual(len(body), 200)

    def test_acknowledge_moves_a_firing_alert_and_stamps_the_operator(self):
        alert = self._alert()
        resp = self.client.post(f"/api/v1/alerts/devices/{alert.id}/acknowledge/", {})
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, DeviceAlert.State.ACKNOWLEDGED)
        self.assertIsNotNone(alert.acknowledged_at)
        self.assertEqual(alert.acknowledged_by, self.user)

    def test_a_second_acknowledge_is_a_400_with_the_host_alerts_wording(self):
        alert = self._alert()
        self.client.post(f"/api/v1/alerts/devices/{alert.id}/acknowledge/", {})
        resp = self.client.post(f"/api/v1/alerts/devices/{alert.id}/acknowledge/", {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "Only a firing alert can be acknowledged.")

    def test_resolving_is_not_acknowledgeable_and_an_unknown_id_is_404(self):
        alert = self._alert()
        add_result(self.check, True)
        evaluate({self.check.id})
        resp = self.client.post(f"/api/v1/alerts/devices/{alert.id}/acknowledge/", {})
        self.assertEqual(resp.status_code, 400)
        import uuid
        self.assertEqual(
            self.client.post(f"/api/v1/alerts/devices/{uuid.uuid4()}/acknowledge/", {}).status_code,
            404)

    def test_a_viewer_acknowledges_as_on_a_host_alert(self):
        """The host acknowledge needs only a login and the alert in scope; same here."""
        UserProfile.objects.filter(user=self.user).update(role=Role.VIEWER)
        alert = self._alert()
        resp = self.client.post(f"/api/v1/alerts/devices/{alert.id}/acknowledge/", {})
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, DeviceAlert.State.ACKNOWLEDGED)

    def test_the_list_needs_only_a_login(self):
        UserProfile.objects.filter(user=self.user).update(role=Role.VIEWER)
        self._alert()
        resp = self.client.get("/api/v1/alerts/devices/")
        self.assertEqual(resp.status_code, 200, resp.content)


class PruneTests(TestCase):
    def setUp(self):
        self.device = make_device()
        self.check = make_check(self.device)

    def test_a_resolved_alert_past_the_cutoff_goes_and_a_firing_one_stays(self):
        add_result(self.check, False, seconds_ago=20)
        add_result(self.check, False, seconds_ago=10)
        evaluate({self.check.id})
        alert = DeviceAlert.objects.get()
        resolved = DeviceAlert.objects.create(
            device=self.device, device_check=self.check,
            state=DeviceAlert.State.RESOLVED, message="an old outage",
            severity="critical")
        cutoff_days = 30
        with patch("apps.alerts.tasks.setting", return_value=str(cutoff_days)):
            resolved.resolved_at = timezone.now() - timedelta(days=cutoff_days + 1)
            resolved.save(update_fields=["resolved_at"])
            summary = prune_old_alerts()
        self.assertNotIn(resolved.id, [a.id for a in DeviceAlert.objects.all()])
        self.assertIn(alert.id, [a.id for a in DeviceAlert.objects.all()])
        self.assertIn("1", summary)

    def test_a_resolved_alert_inside_the_window_stays(self):
        resolved = DeviceAlert.objects.create(
            device=self.device, device_check=self.check,
            state=DeviceAlert.State.RESOLVED, message="a recent outage",
            severity="critical", resolved_at=timezone.now() - timedelta(days=1))
        with patch("apps.alerts.tasks.setting", return_value="30"):
            prune_old_alerts()
        self.assertTrue(DeviceAlert.objects.filter(pk=resolved.pk).exists())


class PageSourceTests(SimpleTestCase):
    """The Devices block renders poller text; the page has a CSP, and no stripe."""

    JS = (JS / "vigil-alerts.js").read_text(encoding="utf-8")

    def test_the_device_row_builder_never_touches_innerhtml(self):
        bodies = self.JS.split("\nfunction ")
        device = [b for b in bodies if b.startswith(("_renderDeviceAlerts", "_deviceAlertRow"))]
        self.assertEqual(len(device), 2, "the device block is missing from vigil-alerts.js")
        for body in device:
            name = body.split("(")[0]
            self.assertNotIn("innerHTML", body.split("\n}\n")[0],
                             f"{name} must build nodes with textContent")
        # The loader and the action too: nothing in this feature touches markup.
        for name in ("refreshDeviceAlerts", "acknowledgeDeviceAlert"):
            self.assertRegex(self.JS, rf"(async )?function {name}\(")

    def test_the_device_block_is_in_the_page_and_loaded_with_the_list(self):
        html = (TEMPLATES / "pages" / "_alerts.html").read_text(encoding="utf-8")
        for element_id in ("device-alerts-block", "device-alerts-list"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="device-alerts-block" hidden', html)
        # Refreshed with the alert list, not only on its own schedule.
        loader = self.JS.split("async function refreshAlerts()")[1].split("\n}")[0]
        self.assertIn("refreshDeviceAlerts()", loader)

    def test_acknowledge_has_one_handler_and_carries_the_id(self):
        """The page dispatcher already runs every [data-act] click; a second
        delegateClick on the same button would POST twice."""
        self.assertIn("ack.dataset.a1 = alert.id", self.JS)
        self.assertNotIn('delegateClick(\'[data-act="acknowledgeDeviceAlert"]', self.JS)

    def test_no_device_alert_rule_adds_a_side_stripe(self):
        css = design_css()
        rules = re.findall(r"(\.device-alert[^{]*)\{([^}]*)\}", css)
        self.assertTrue(rules, "the device alert CSS is missing")
        for selector, body in rules:
            for side in ("border-left", "border-right", "border-top", "border-bottom"):
                self.assertNotIn(side, body, f"{selector.strip()} grew a {side}")
