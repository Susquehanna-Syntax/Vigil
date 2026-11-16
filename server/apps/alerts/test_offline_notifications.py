"""Alerts a host going offline, and coming back, sends to the channels.

The rule'd path always notified; the unrule'd "Host offline" alert that
mark_stale_hosts_offline creates did not, so the most-cared-about page never
reached Slack or email. And an agent's check-in resolved the alert silently —
a slow channel must not hold up a check-in, so the notice is left owing and
the next pass of the stale-host job sends it.
"""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.hosts.models import Host

from .models import Alert
from .tasks import _mark_stale_hosts_offline

DISPATCH = "apps.alerts.tasks.dispatch_alert_notification"


class OfflineNotificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dispatch = Mock()
        patcher = patch(DISPATCH, self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stale_host(self, hostname="h1", secret="a"):
        return Host.objects.create(
            hostname=hostname, agent_token=f"tok-{secret}" + "a" * 28, os="linux",
            status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL,
            last_checkin=now() - timedelta(minutes=10))

    def _checkin(self, host):
        return self.client.post(
            "/api/v1/checkin", {"hostname": host.hostname}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}")

    def _offline_alerts(self, host):
        return Alert.objects.filter(host=host, rule=None)

    def test_stale_host_fires_once_and_the_channels_hear_it(self):
        host = self._stale_host()
        result = _mark_stale_hosts_offline()
        alerts = list(self._offline_alerts(host))
        self.assertEqual(len(alerts), 1)
        self.dispatch.assert_called_once_with(alerts[0], event="firing")
        self.assertIsNotNone(alerts[0].notified_at)
        self.assertEqual(result, "1 hosts marked offline, 0 back-online notices sent")

    def test_second_pass_is_silent(self):
        host = self._stale_host()
        _mark_stale_hosts_offline()
        self.dispatch.reset_mock()
        _mark_stale_hosts_offline()
        self.assertEqual(self._offline_alerts(host).count(), 1)
        self.dispatch.assert_not_called()

    def test_host_in_a_maintenance_window_alerts_and_sends_nothing(self):
        # The same window a reprovisioning rebuild sets; MaintenanceWindow
        # rows are covered by apps/reprovision/test_maintenance.py.
        host = Host.objects.create(
            hostname="maint", agent_token="tok-b" + "a" * 28, os="linux",
            status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL,
            last_checkin=now() - timedelta(minutes=10),
            maintenance_until=now() + timedelta(minutes=30))
        _mark_stale_hosts_offline()
        self.assertFalse(Alert.objects.filter(host=host).exists())
        self.dispatch.assert_not_called()

    def test_host_in_a_scheduled_window_is_silent_going_and_coming_back(self):
        """A MaintenanceWindow (not maintenance_until) is decided inside
        dispatch, so the alert exists; nothing may be sent at either edge."""
        from .models import MaintenanceWindow
        from .notifications import dispatch_alert_notification as real
        self.dispatch.side_effect = real
        host = self._stale_host()
        window = MaintenanceWindow.objects.create(
            name="patching", kind=MaintenanceWindow.Kind.ONCE,
            starts_at=now() - timedelta(minutes=30), ends_at=now() + timedelta(hours=1))
        window.hosts.add(host)
        with patch("apps.alerts.notifications.deliver") as deliver:
            _mark_stale_hosts_offline()
            alert = self._offline_alerts(host).get()
            self.assertIsNone(alert.notified_at)
            window.delete()
            self._checkin(host)
            result = _mark_stale_hosts_offline()
        deliver.assert_not_called()
        self.assertIn("0 back-online notices sent", result)

    def test_checkin_resolves_without_sending(self):
        host = self._stale_host()
        _mark_stale_hosts_offline()
        self.dispatch.reset_mock()
        self.assertEqual(self._checkin(host).status_code, 200)
        alert = self._offline_alerts(host).get()
        self.assertEqual(alert.state, Alert.State.RESOLVED)
        self.assertIsNotNone(alert.resolved_at)
        self.assertTrue(alert.resolve_pending)
        self.dispatch.assert_not_called()

    def test_acknowledged_offline_alert_resolves_on_checkin(self):
        host = self._stale_host()
        _mark_stale_hosts_offline()
        alert = self._offline_alerts(host).get()
        alert.state = Alert.State.ACKNOWLEDGED
        alert.save(update_fields=["state"])
        self.assertEqual(self._checkin(host).status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.RESOLVED)
        self.assertTrue(alert.resolve_pending)

    def test_next_pass_sends_the_owed_resolution_once(self):
        host = self._stale_host()
        _mark_stale_hosts_offline()
        self._checkin(host)
        self.dispatch.reset_mock()
        first = _mark_stale_hosts_offline()
        alert = self._offline_alerts(host).get()
        self.dispatch.assert_called_once_with(alert, event="resolved")
        self.assertIn("1 back-online notices sent", first)
        alert.refresh_from_db()
        self.assertFalse(alert.resolve_pending)
        self.dispatch.reset_mock()
        second = _mark_stale_hosts_offline()
        self.dispatch.assert_not_called()
        self.assertIn("0 back-online notices sent", second)

    def test_owed_resolution_never_notified_is_cleared_silently(self):
        host = self._stale_host()
        _mark_stale_hosts_offline()
        alert = self._offline_alerts(host).get()
        Alert.objects.filter(pk=alert.pk).update(notified_at=None, resolve_pending=True)
        self.dispatch.reset_mock()
        _mark_stale_hosts_offline()
        self.dispatch.assert_not_called()
        alert.refresh_from_db()
        self.assertFalse(alert.resolve_pending)
