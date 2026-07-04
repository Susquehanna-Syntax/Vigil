from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.hosts.models import Host

from .models import Alert
from .tasks import expire_acknowledgements


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
