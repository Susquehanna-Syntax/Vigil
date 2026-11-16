"""Hosts behind another host: while the parent is offline, the child's offline
alert exists but sends nothing, so one outage is one page. When the parent
comes back and the child is still down, the child's notice goes out then; the
same sweep covers a notice a maintenance window held back."""

import importlib
from datetime import timedelta
from unittest.mock import Mock, patch

from django.apps import apps as global_apps
from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host

from .models import Alert, AlertRule
from .tasks import _mark_stale_hosts_offline

DISPATCH = "apps.alerts.tasks.dispatch_alert_notification"


class HostDependencyTests(TestCase):
    def setUp(self):
        self.dispatch = Mock(return_value=True)
        patcher = patch(DISPATCH, self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _host(self, hostname, status=Host.Status.ONLINE, depends_on=None,
              stale=True):
        return Host.objects.create(
            hostname=hostname, agent_token=f"tok-{hostname}" + "a" * 28,
            os="linux", status=status, mode=Host.Mode.FULL_CONTROL,
            last_checkin=(now() - timedelta(minutes=10)) if stale else now(),
            depends_on=depends_on)

    def _back_online(self, host):
        # Not a check-in: that would resolve the offline alert too. The parent
        # comes back and its row goes fresh so it is not re-marked offline.
        Host.objects.filter(pk=host.pk).update(status=Host.Status.ONLINE,
                                               last_checkin=now())

    def _offline_alert(self, host):
        return Alert.objects.get(host=host, rule=None)

    def test_parent_and_child_going_stale_together_page_once(self):
        parent = self._host("switch")
        child = self._host("vm1", depends_on=parent)
        _mark_stale_hosts_offline()
        parent_alert = self._offline_alert(parent)
        child_alert = self._offline_alert(child)
        self.assertEqual(Alert.objects.filter(rule=None).count(), 2)
        self.dispatch.assert_called_once_with(parent_alert, event="firing")
        self.assertIsNone(child_alert.notified_at)
        self.assertIn("depends on switch", child_alert.message)

    def test_child_going_stale_behind_an_already_offline_parent_sends_nothing(self):
        parent = self._host("switch2", status=Host.Status.OFFLINE, stale=False)
        child = self._host("vm2", depends_on=parent)
        _mark_stale_hosts_offline()
        alert = self._offline_alert(child)
        self.assertIsNone(alert.notified_at)
        self.dispatch.assert_not_called()

    def test_child_with_no_parent_is_paged_as_before(self):
        host = self._host("lonely")
        _mark_stale_hosts_offline()
        self.dispatch.assert_called_once_with(self._offline_alert(host),
                                              event="firing")
        self.assertIsNotNone(self._offline_alert(host).notified_at)

    def test_child_behind_an_online_parent_is_paged_as_before(self):
        parent = self._host("switch3", stale=False)
        child = self._host("vm3", depends_on=parent)
        _mark_stale_hosts_offline()
        alert = self._offline_alert(child)
        self.dispatch.assert_called_once_with(alert, event="firing")
        self.assertIsNotNone(alert.notified_at)

    def test_parent_coming_back_pages_the_still_down_child_once(self):
        parent = self._host("switch4")
        child = self._host("vm4", depends_on=parent)
        _mark_stale_hosts_offline()
        self.dispatch.reset_mock()
        self._back_online(parent)
        _mark_stale_hosts_offline()
        alert = self._offline_alert(child)
        self.dispatch.assert_called_once_with(alert, event="firing")
        alert.refresh_from_db()
        self.assertIsNotNone(alert.notified_at)
        self.dispatch.reset_mock()
        _mark_stale_hosts_offline()
        self.dispatch.assert_not_called()

    def test_the_late_sweep_leaves_an_acknowledged_alert_alone(self):
        parent = self._host("switch5")
        child = self._host("vm5", depends_on=parent)
        _mark_stale_hosts_offline()
        self._back_online(parent)
        Alert.objects.filter(host=child, rule=None).update(
            state=Alert.State.ACKNOWLEDGED)
        self.dispatch.reset_mock()
        _mark_stale_hosts_offline()
        self.dispatch.assert_not_called()

    def test_checking_in_while_held_back_pages_nothing_on_either_edge(self):
        """An offline notice nobody was told about says nothing when the host
        returns either, whatever pass its owed resolution is cleared in."""
        parent = self._host("switch7")
        child = self._host("vm7", depends_on=parent)
        _mark_stale_hosts_offline()
        alert = self._offline_alert(child)
        self.assertIsNone(alert.notified_at)
        self.dispatch.reset_mock()
        Alert.objects.filter(pk=alert.pk).update(state=Alert.State.RESOLVED,
                                                 resolved_at=now(),
                                                 resolve_pending=True)
        child.last_checkin = now()
        child.save(update_fields=["last_checkin"])
        result = _mark_stale_hosts_offline()
        alert.refresh_from_db()
        self.dispatch.assert_not_called()
        self.assertFalse(alert.resolve_pending)
        self.assertIn("0 back-online notices sent", result)

    def test_the_late_sweep_waits_for_a_maintenance_window_to_close(self):
        """The real dispatch, so the window is what decides, as in the
        maintenance test of test_offline_notifications."""
        from .models import MaintenanceWindow, NotificationChannel
        from .notifications import dispatch_alert_notification

        child = self._host("vm6")
        window = MaintenanceWindow.objects.create(
            name="patching", kind=MaintenanceWindow.Kind.ONCE,
            starts_at=now() - timedelta(minutes=30), ends_at=now() + timedelta(hours=1))
        window.hosts.add(child)
        # deliver() needs a channel to hear through; the child is the only host
        # in the window, so every delivery below is about it.
        NotificationChannel.objects.create(
            name="urlhook", kind="webhook",
            config={"webhook_url": "https://example.invalid/hook"})
        with patch(DISPATCH, side_effect=dispatch_alert_notification), \
                patch("apps.alerts.notifications.deliver") as deliver:
            _mark_stale_hosts_offline()
            # The window held the notice back on its way down.
            self.assertIsNone(self._offline_alert(child).notified_at)
            deliver.reset_mock()
            _mark_stale_hosts_offline()
            deliver.assert_not_called()
            window.delete()
            _mark_stale_hosts_offline()
        alert = self._offline_alert(child)
        self.assertIsNotNone(alert.notified_at)
        self.assertEqual([c.args[1].host for c in deliver.call_args_list],
                         [child.hostname])


class AlreadyNotifiedMigrationTests(TestCase):
    def test_forward_marks_open_offline_alerts_already_sent(self):
        migration = importlib.import_module(
            "apps.alerts.migrations.0012_offline_alerts_already_notified")
        host = Host.objects.create(hostname="old", agent_token="tok-old" + "a" * 28,
                                   os="linux", status=Host.Status.OFFLINE,
                                   mode=Host.Mode.FULL_CONTROL)
        open_alert = Alert.objects.create(host=host, rule=None,
                                          state=Alert.State.FIRING,
                                          severity="warning",
                                          message="Host offline: old has gone",
                                          metric_value=None)
        resolved = Alert.objects.create(host=host, rule=None,
                                        state=Alert.State.RESOLVED,
                                        severity="warning",
                                        message="Host offline: old has gone",
                                        metric_value=None)
        rule_alert = Alert.objects.create(
            host=host, rule=AlertRule.objects.create(name="r", category="cpu",
                                                     metric="cpu", operator="gt",
                                                     threshold=90, severity="warning"),
            state=Alert.State.FIRING, severity="warning",
            message="cpu high", metric_value=95.0)

        migration.forward(global_apps, None)

        open_alert.refresh_from_db()
        resolved.refresh_from_db()
        rule_alert.refresh_from_db()
        self.assertIsNotNone(open_alert.notified_at)
        self.assertIsNone(resolved.notified_at)
        self.assertIsNone(rule_alert.notified_at)
