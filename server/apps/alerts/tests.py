from datetime import timedelta

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint

from .models import Alert, AlertRule
from .tasks import (FLAP_WINDOW_SECONDS, evaluate_alert_rules,
                    expire_acknowledgements, prune_old_alerts)


class AlertAckLifecycleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )

    def _alert(self, **kw):
        return Alert.objects.create(
            host=self.host,
            rule=None,
            state=kw.pop("state", Alert.State.FIRING),
            severity="warning",
            message=kw.pop("message", "Docker: Container 'web' is running an outdated image"),
            **kw,
        )

    def test_acknowledge_forever_by_default(self):
        alert = self._alert()
        resp = self.client.post(f"/api/v1/alerts/{alert.id}/acknowledge/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)
        self.assertIsNone(alert.acknowledged_until)

    def test_acknowledge_with_duration_sets_expiry(self):
        alert = self._alert()
        resp = self.client.post(
            f"/api/v1/alerts/{alert.id}/acknowledge/",
            {"duration_seconds": 3600},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        alert.refresh_from_db()
        self.assertIsNotNone(alert.acknowledged_until)
        remaining = (alert.acknowledged_until - now()).total_seconds()
        self.assertGreater(remaining, 3500)
        self.assertLessEqual(remaining, 3600)

    def test_acknowledge_rejects_bad_duration(self):
        alert = self._alert()
        for bad in ("soon", -5, 0):
            resp = self.client.post(
                f"/api/v1/alerts/{alert.id}/acknowledge/",
                {"duration_seconds": bad},
                format="json",
            )
            self.assertEqual(resp.status_code, 400, bad)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)

    def test_unacknowledge_refires(self):
        alert = self._alert(
            state=Alert.State.ACKNOWLEDGED,
            acknowledged_at=now(),
            acknowledged_until=now() + timedelta(hours=1),
        )
        resp = self.client.post(f"/api/v1/alerts/{alert.id}/unacknowledge/")
        self.assertEqual(resp.status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertIsNone(alert.acknowledged_at)
        self.assertIsNone(alert.acknowledged_until)

    def test_unacknowledge_requires_acknowledged_state(self):
        alert = self._alert()
        resp = self.client.post(f"/api/v1/alerts/{alert.id}/unacknowledge/")
        self.assertEqual(resp.status_code, 400)


class AlertBulkActionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )

    def _alert(self, state=Alert.State.FIRING, **kw):
        return Alert.objects.create(
            host=self.host, rule=None, state=state, severity="warning",
            message="Docker: Container 'web' is running an outdated image", **kw,
        )

    def _bulk(self, ids, action, **extra):
        return self.client.post(
            "/api/v1/alerts/bulk/",
            {"ids": ids, "action": action, **extra},
            format="json",
        )

    def test_bulk_acknowledge_with_duration(self):
        alerts = [self._alert() for _ in range(3)]
        resp = self._bulk([str(a.id) for a in alerts], "acknowledge", duration_seconds=3600)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["updated"], 3)
        self.assertEqual(resp.data["skipped"], 0)
        for a in alerts:
            a.refresh_from_db()
            self.assertEqual(a.state, Alert.State.ACKNOWLEDGED)
            self.assertIsNotNone(a.acknowledged_until)

    def test_bulk_skips_wrong_state_and_bad_ids(self):
        firing = self._alert()
        acked = self._alert(state=Alert.State.ACKNOWLEDGED, acknowledged_at=now())
        resp = self._bulk([str(firing.id), str(acked.id), "not-a-uuid"], "acknowledge")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["updated"], 1)
        self.assertEqual(resp.data["skipped"], 2)

    def test_bulk_unacknowledge(self):
        acked = [
            self._alert(state=Alert.State.ACKNOWLEDGED, acknowledged_at=now(),
                        acknowledged_until=now() + timedelta(hours=1))
            for _ in range(2)
        ]
        resp = self._bulk([str(a.id) for a in acked], "unacknowledge")
        self.assertEqual(resp.data["updated"], 2)
        for a in acked:
            a.refresh_from_db()
            self.assertEqual(a.state, Alert.State.FIRING)
            self.assertIsNone(a.acknowledged_until)

    def test_bulk_validates_input(self):
        self.assertEqual(self._bulk([], "acknowledge").status_code, 400)
        self.assertEqual(self._bulk([str(self._alert().id)], "resolve").status_code, 400)
        self.assertEqual(
            self._bulk([str(self._alert().id)], "acknowledge", duration_seconds=-5).status_code,
            400,
        )


class ExpireAcknowledgementsTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )

    def _acked(self, until):
        return Alert.objects.create(
            host=self.host,
            rule=None,
            state=Alert.State.ACKNOWLEDGED,
            severity="warning",
            message="Docker: Container 'web' is running an outdated image",
            acknowledged_at=now() - timedelta(hours=2),
            acknowledged_until=until,
        )

    def test_expired_ack_refires(self):
        alert = self._acked(now() - timedelta(minutes=1))
        expire_acknowledgements()
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertIsNone(alert.acknowledged_until)

    def test_future_ack_untouched(self):
        alert = self._acked(now() + timedelta(hours=1))
        expire_acknowledgements()
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)

    def test_permanent_ack_untouched(self):
        alert = self._acked(None)
        expire_acknowledgements()
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)


class FlapSuppressionTests(TestCase):
    """A metric oscillating across its threshold used to page on every cycle.

    The existing-alert check only ever stopped two simultaneous FIRING rows;
    the breach → resolve → breach cycle created a fresh row and a fresh
    notification each time, which is how one bad disk produced thousands of
    pages a day and taught an operator to ignore the channel.
    """

    def setUp(self):
        self.host = Host.objects.create(
            hostname="box", ip_address="10.0.0.11", agent_token="tok-flap",
            mode="managed", status=Host.Status.ONLINE)
        # Vigil ships 20 default rules in migrations, several of which watch
        # disk usage — leave them enabled and this test measures theirs too.
        AlertRule.objects.update(enabled=False)
        self.rule = AlertRule.objects.create(
            name="Flap probe", category="disk", metric="usage_percent",
            operator="gt", threshold=90.0, severity="warning", enabled=True,
            duration_seconds=0)

    def _point(self, value, seconds_ago=0):
        MetricPoint.objects.create(
            host=self.host, time=now() - timedelta(seconds=seconds_ago),
            category="disk", metric="usage_percent", value=value)

    def _evaluate(self):
        with patch("apps.alerts.tasks.dispatch_alert_notification") as notify:
            evaluate_alert_rules()
        return notify

    def test_a_flap_reopens_the_same_alert_without_paging_again(self):
        self._point(95.0)
        first = self._evaluate()
        self.assertEqual(first.call_count, 1)
        self.assertEqual(Alert.objects.count(), 1)

        # Recover.
        MetricPoint.objects.all().delete()
        self._point(50.0)
        self._evaluate()
        self.assertEqual(Alert.objects.get().state, Alert.State.RESOLVED)

        # Breach again, inside the window.
        MetricPoint.objects.all().delete()
        self._point(96.0)
        third = self._evaluate()

        self.assertEqual(Alert.objects.count(), 1,
                         "the flap created a second alert row")
        alert = Alert.objects.get()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertEqual(alert.flap_count, 1)
        self.assertEqual(third.call_count, 0,
                         "the flap paged the operator a second time")

    def test_a_breach_after_the_window_is_a_new_alert_and_does_page(self):
        """Suppression must not swallow a genuinely new problem later on."""
        self._point(95.0)
        self._evaluate()
        MetricPoint.objects.all().delete()
        self._point(50.0)
        self._evaluate()

        stale = Alert.objects.get()
        stale.resolved_at = now() - timedelta(seconds=FLAP_WINDOW_SECONDS + 60)
        stale.save(update_fields=["resolved_at"])

        MetricPoint.objects.all().delete()
        self._point(97.0)
        again = self._evaluate()

        self.assertEqual(Alert.objects.count(), 2)
        self.assertEqual(again.call_count, 1)


class AlertRetentionTests(TestCase):
    """alerts_alert was the one growing table nothing ever trimmed."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="box", ip_address="10.0.0.12", agent_token="tok-ret",
            mode="managed", status=Host.Status.ONLINE)

    def _alert(self, state, resolved_days_ago=None):
        return Alert.objects.create(
            host=self.host, state=state, severity="warning", message="m",
            resolved_at=(now() - timedelta(days=resolved_days_ago)
                         if resolved_days_ago is not None else None))

    def test_old_resolved_alerts_are_pruned(self):
        self._alert(Alert.State.RESOLVED, resolved_days_ago=200)
        self._alert(Alert.State.RESOLVED, resolved_days_ago=10)
        prune_old_alerts()
        self.assertEqual(Alert.objects.count(), 1)

    def test_live_alerts_are_never_pruned(self):
        """Firing and acknowledged alerts are state, not history."""
        self._alert(Alert.State.FIRING)
        self._alert(Alert.State.ACKNOWLEDGED)
        old = self._alert(Alert.State.RESOLVED, resolved_days_ago=999)
        prune_old_alerts()
        self.assertEqual(Alert.objects.count(), 2)
        self.assertFalse(Alert.objects.filter(pk=old.pk).exists())

    @override_settings(VIGIL_ALERT_RETENTION_DAYS=0)
    def test_retention_can_be_switched_off(self):
        self._alert(Alert.State.RESOLVED, resolved_days_ago=999)
        prune_old_alerts()
        self.assertEqual(Alert.objects.count(), 1)
