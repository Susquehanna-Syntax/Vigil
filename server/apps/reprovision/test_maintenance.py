"""Alert suppression while a host is rebuilding (§6).

The alert that matters is the one mark_stale_hosts_offline raises: a
rebuilding host stops checking in, so within five minutes it is marked
offline and an unrule'd "Host offline" alert fires. That is precisely the
page this feature must not send.
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.alerts.models import Alert
from apps.alerts.tasks import mark_stale_hosts_offline
from apps.hosts.models import Host


def stale_host(hostname, **kw):
    """An online host that last checked in long enough ago to be swept."""
    return Host.objects.create(
        hostname=hostname, agent_token=f"tok-{uuid.uuid4()}",
        status=Host.Status.ONLINE,
        last_checkin=now() - timedelta(minutes=30), **kw)


class MaintenanceSuppressionTests(TestCase):
    def test_no_offline_alert_inside_the_window(self):
        host = stale_host("rebuilding",
                          maintenance_until=now() + timedelta(minutes=30))
        mark_stale_hosts_offline()
        self.assertFalse(Alert.objects.filter(host=host).exists())

    def test_host_is_still_marked_offline_inside_the_window(self):
        # It genuinely is down — suppress the page, not the truth.
        host = stale_host("rebuilding2",
                          maintenance_until=now() + timedelta(minutes=30))
        mark_stale_hosts_offline()
        host.refresh_from_db()
        self.assertEqual(host.status, Host.Status.OFFLINE)

    def test_offline_alert_fires_for_a_host_with_no_window(self):
        host = stale_host("ordinary")
        mark_stale_hosts_offline()
        self.assertTrue(Alert.objects.filter(host=host).exists())

    def test_offline_alert_fires_once_the_window_has_lapsed(self):
        host = stale_host("lapsed",
                          maintenance_until=now() - timedelta(minutes=1))
        mark_stale_hosts_offline()
        self.assertTrue(Alert.objects.filter(host=host).exists())

    def test_rule_evaluation_skips_hosts_in_a_window(self):
        from apps.alerts.models import AlertRule
        from apps.alerts.tasks import evaluate_alert_rules

        AlertRule.objects.create(
            name="always", metric="cpu_percent", operator="gt", threshold=-1,
            severity="warning", enabled=True)
        Host.objects.create(
            hostname="rb", agent_token=f"tok-{uuid.uuid4()}",
            status=Host.Status.ONLINE, last_checkin=now(),
            maintenance_until=now() + timedelta(minutes=30))
        evaluate_alert_rules()
        self.assertFalse(Alert.objects.filter(host__hostname="rb").exists())
