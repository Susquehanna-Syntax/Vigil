"""Background work follows sites (S1 05b, decisions-06 items 7 and 18).

What a background job sends out reaches only those who may see it: a Lab
alert never reaches a West notification channel, a task's notification
never crosses to another site's channel, and a scheduled policy touches only
its own site's hosts.
"""
from unittest import mock

from django.test import TestCase

from apps.alerts.models import Alert, AlertRule, NotificationChannel
from apps.hosts.models import Host
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import ChannelSiteAssignment, HostSiteAssignment, Site  # noqa: E402


class _Fleet(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab), ("glob-1", None)):
            host = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                       status=Host.Status.ONLINE, mode="managed")
            if site is not None:
                HostSiteAssignment.objects.create(host=host, site=site)
            self.hosts[name] = host
        self.channels = {}
        for name, site in (("west-ch", self.west), ("lab-ch", self.lab), ("all-ch", None)):
            channel = NotificationChannel.objects.create(
                name=name, kind=NotificationChannel.Kind.WEBHOOK,
                config={"url": f"https://hooks.example/{name}"})
            if site is not None:
                ChannelSiteAssignment.objects.create(channel=channel, site=site)
            self.channels[name] = channel


class ChannelDeliveryTests(_Fleet):
    def heard(self, host):
        from apps.alerts.notifications import dispatch_alert_notification

        rule = AlertRule.objects.create(name="cpu", metric="cpu_percent", operator="gt",
                                        threshold=90, severity=AlertRule.Severity.WARNING)
        alert = Alert.objects.create(host=host, rule=rule, severity="warning", message="hot")
        sent = []
        with mock.patch("apps.alerts.notifications._send_webhook",
                        side_effect=lambda ch, payload: sent.append(ch.name)), \
                mock.patch.dict("apps.alerts.notifications._DISPATCHERS",
                                {NotificationChannel.Kind.WEBHOOK:
                                 lambda ch, payload: sent.append(ch.name)}):
            with scoping.system(reason="test: alert delivery runs as a job"):
                dispatch_alert_notification(alert, "firing")
        return sorted(sent)

    def test_a_lab_alert_reaches_lab_and_global_channels_only(self):
        self.assertEqual(self.heard(self.hosts["lab-1"]), ["all-ch", "lab-ch"])

    def test_a_west_alert_reaches_west_and_global_channels_only(self):
        self.assertEqual(self.heard(self.hosts["west-1"]), ["all-ch", "west-ch"])

    def test_a_global_hosts_alert_reaches_global_channels_only(self):
        self.assertEqual(self.heard(self.hosts["glob-1"]), ["all-ch"])


class TaskNotificationTests(_Fleet):
    def test_a_lab_task_cannot_name_a_west_channel(self):
        from apps.alerts.notifications import _channels_for_host

        with scoping.system(reason="test"):
            named = NotificationChannel.objects.filter(name="west-ch")
            self.assertEqual(_channels_for_host(named, self.hosts["lab-1"]), [])
            named = NotificationChannel.objects.filter(name="all-ch")
            self.assertEqual([c.name for c in _channels_for_host(named, self.hosts["lab-1"])], ["all-ch"])


class ScheduledPolicyTests(_Fleet):
    def test_a_west_policy_covers_west_hosts_only(self):
        # decisions-06 item 18: bounded by the policy's own site, whoever made it.
        from apps.policies.drift import policy_hosts
        from apps.policies.models import UpdatePolicy

        policy = UpdatePolicy.objects.create(name="West patch", site_id=self.west.pk)
        with scoping.system(reason="test: the beat runs scheduled policies"):
            self.assertEqual([h.hostname for h in policy_hosts(policy)], ["west-1"])
