"""Hosts of one site that go offline in the same pass send one notice.

A site that loses its uplink stops every host there checking in at about the
same moment, and the stale-host job used to page once per host. From GROUP_MIN
hosts of one site onward the channels hear one grouped notice instead; below
it, one each as before. Back-online notices group the same way.
"""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from vigil import scoping

from .models import Alert, MaintenanceWindow, NotificationChannel
from .notifications import dispatch_offline_group
from .tasks import _mark_stale_hosts_offline

DISPATCH = "apps.alerts.tasks.dispatch_alert_notification"
GROUP = "apps.alerts.tasks.dispatch_offline_group"


class GroupedOfflineDispatchTests(TestCase):
    def setUp(self):
        self.dispatch = Mock(return_value=True)
        patcher = patch(DISPATCH, self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.group = Mock(side_effect=lambda alerts, event: list(alerts))
        patcher = patch(GROUP, self.group)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stale_host(self, hostname):
        # 10 minutes stale, the way the other offline-notice tests do it; a
        # resolved alert created in the same second would read as not having
        # lasted 5 minutes, so the back-online one is created backdated.
        return Host.objects.create(
            hostname=hostname, agent_token=f"tok-{hostname}" + "a" * 28,
            os="linux", status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL,
            last_checkin=now() - timedelta(minutes=10))

    def _offline_alerts(self):
        return Alert.objects.filter(rule=None, host__isnull=False,
                                    message__startswith="Host offline:")

    def test_three_stale_hosts_of_one_site_send_one_grouped_notice(self):
        for index in range(3):
            self._stale_host(f"h{index}")
        _mark_stale_hosts_offline()
        self.group.assert_called_once()
        grouped = self.group.call_args.args[0]
        self.assertEqual(len(grouped), 3)
        self.assertEqual(self.group.call_args.kwargs["event"], "firing")
        self.dispatch.assert_not_called()
        for alert in self._offline_alerts():
            self.assertIsNotNone(alert.notified_at)

    def test_two_stale_hosts_still_send_one_notice_each(self):
        for index in range(2):
            self._stale_host(f"h{index}")
        _mark_stale_hosts_offline()
        self.group.assert_not_called()
        self.assertEqual(self.dispatch.call_count, 2)

    def test_a_group_is_one_site(self):
        for index in range(3):
            self._stale_host(f"west{index}")
        other = self._stale_host("east")
        site_of = {"west0": "a", "west1": "a", "west2": "a", "east": "b"}
        with patch("apps.alerts.tasks.scoping.site_id_of",
                   side_effect=lambda host: site_of[host.hostname]):
            _mark_stale_hosts_offline()
        self.group.assert_called_once()
        grouped = self.group.call_args.args[0]
        self.assertEqual({a.host.hostname for a in grouped},
                         {"west0", "west1", "west2"})
        self.assertEqual(self.dispatch.call_count, 1)
        self.assertEqual(self.dispatch.call_args.args[0].host, other)

    def test_only_the_alerts_the_group_notice_covered_are_notified(self):
        for index in range(3):
            self._stale_host(f"h{index}")
        # As if the group dispatch left one host out of the notice, the way it
        # leaves a host in a maintenance window out. The late sweep runs on
        # this same pass and would send the left-out alert's own notice; with
        # nothing holding it back, that is the right thing for it to do - so
        # patch it away and check what this step notified.
        self.group.side_effect = lambda alerts, event: list(alerts)[:2]
        with patch("apps.alerts.tasks._send_late_offline_notices"):
            _mark_stale_hosts_offline()
        notified = sorted(self._offline_alerts()
                          .exclude(notified_at=None)
                          .values_list("host__hostname", flat=True))
        self.assertEqual(len(notified), 2)

    def test_hosts_in_two_sites_still_get_one_notice_each_site(self):
        if scoping._sites_models() is None:
            self.skipTest("the Business sites app is not installed")
        from apps_business.sites.models import HostSiteAssignment, Site

        west = Site.objects.create(name="West Campus", slug="west-campus")
        east = Site.objects.create(name="East Campus", slug="east-campus")
        for index in range(3):
            HostSiteAssignment.objects.create(host=self._stale_host(f"west{index}"),
                                              site=west)
        for index in range(3):
            HostSiteAssignment.objects.create(host=self._stale_host(f"east{index}"),
                                              site=east)
        _mark_stale_hosts_offline()
        self.assertEqual(self.group.call_count, 2)
        # A group is one site's hosts only: West's notice never names an East
        # host, and vice versa.
        for call in self.group.call_args_list:
            grouped = sorted(a.host.hostname for a in call.args[0])
            prefix = grouped[0][:4]
            self.assertEqual(grouped, [f"{prefix}{i}" for i in range(3)])
        self.dispatch.assert_not_called()

    def test_three_back_online_hosts_of_one_site_send_one_grouped_notice(self):
        for index in range(3):
            host = self._stale_host(f"h{index}")
            # What a check-in leaves behind, but set directly: an alert
            # created and resolved in the same second reads as an outage that
            # never lasted 5 minutes, and the sweep clears it as a phantom.
            Host.objects.filter(pk=host.pk).update(status=Host.Status.OFFLINE)
            Alert.objects.create(
                host=host, rule=None, state=Alert.State.RESOLVED,
                severity="warning", notified_at=now(), resolve_pending=True,
                resolved_at=now() - timedelta(minutes=1),
                fired_at=now() - timedelta(minutes=20),
                message=f"Host offline: {host.hostname} has not checked in for 5+ minutes")
        result = _mark_stale_hosts_offline()
        self.group.assert_called_once()
        grouped = self.group.call_args.args[0]
        self.assertEqual(len(grouped), 3)
        self.assertEqual(self.group.call_args.kwargs["event"], "resolved")
        self.dispatch.assert_not_called()
        self.assertFalse(self._offline_alerts().filter(resolve_pending=True).exists())
        self.assertTrue(result.endswith("3 back-online notices sent"))


class GroupedNoticeTests(TestCase):
    """The grouped notice itself, with the real dispatch."""

    def setUp(self):
        NotificationChannel.objects.create(
            name="urlhook", kind="webhook",
            config={"webhook_url": "https://example.invalid/hook"})

    def _alerts(self, count):
        alerts = []
        for index in range(count):
            # Backdated so an open-ended outage never looks like a phantom to
            # any pass that reads it; dispatch itself ignores both timestamps.
            hostname = f"host{index:02d}"
            host = Host.objects.create(
                hostname=hostname, agent_token=f"tok-{hostname}" + "a" * 28,
                os="linux", status=Host.Status.OFFLINE,
                mode=Host.Mode.FULL_CONTROL,
                last_checkin=now() - timedelta(minutes=30),
            )
            alerts.append(Alert.objects.create(
                host=host, rule=None, state=Alert.State.FIRING,
                severity="warning", fired_at=now() - timedelta(minutes=30),
                message=f"Host offline: {hostname} has not checked in"))
        return alerts

    def test_a_site_names_itself_in_the_notice(self):
        if scoping._sites_models() is None:
            self.skipTest("the Business sites app is not installed")
        from apps_business.sites.models import HostSiteAssignment, Site

        site = Site.objects.create(name="West Campus", slug="west-campus")
        alerts = self._alerts(3)
        for alert in alerts:
            HostSiteAssignment.objects.create(host=alert.host, site=site)
        with patch("apps.alerts.notifications.deliver") as deliver:
            dispatch_offline_group(alerts, event="firing")
        message = deliver.call_args.args[1]
        self.assertEqual(message.text,
                         "3 hosts in West Campus stopped checking in: "
                         "host00, host01, host02")

    def test_a_long_group_names_ten_hosts_and_counts_the_rest(self):
        alerts = self._alerts(12)
        with patch("apps.alerts.notifications.deliver") as deliver:
            covered = dispatch_offline_group(alerts, event="firing")
        self.assertEqual(len(covered), 12)
        deliver.assert_called_once()
        message = deliver.call_args.args[1]
        names = sorted(a.host.hostname for a in alerts)
        self.assertEqual(message.text,
                         "12 hosts stopped checking in: "
                         f"{', '.join(names[:10])} and 2 more")
        self.assertEqual(message.title, "Alert: 12 hosts")
        self.assertEqual(message.payload["hosts"], names)
        self.assertEqual(message.payload["message"], message.text)

    def test_a_group_notice_of_two_names_them_all(self):
        alerts = self._alerts(2)
        with patch("apps.alerts.notifications.deliver") as deliver:
            dispatch_offline_group(alerts, event="resolved")
        message = deliver.call_args.args[1]
        self.assertEqual(message.text,
                         "2 hosts are checking in again: host00, host01")

    def test_every_host_in_a_window_gets_nothing(self):
        alerts = self._alerts(3)
        window = MaintenanceWindow.objects.create(
            name="patching", kind=MaintenanceWindow.Kind.ONCE,
            starts_at=now() - timedelta(minutes=30), ends_at=now() + timedelta(hours=1))
        for alert in alerts:
            window.hosts.add(alert.host)
        with patch("apps.alerts.notifications.deliver") as deliver:
            covered = dispatch_offline_group(alerts, event="firing")
        self.assertEqual(covered, [])
        deliver.assert_not_called()
