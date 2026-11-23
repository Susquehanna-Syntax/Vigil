"""The ZFS default rules ship via migration and fire on the host-level summary points."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint

from .models import AlertRule
from .tasks import _evaluate_alert_rules

ZFS_RULE_NAMES = [
    "ZFS pool not healthy",
    "ZFS pool nearly full (85%)",
    "ZFS pool errors",
    "ZFS scrub overdue (35 days)",
]


class ZfsDefaultRulesTests(TestCase):
    def test_the_four_zfs_rules_exist_as_defaults(self):
        for name in ZFS_RULE_NAMES:
            rule = AlertRule.objects.get(name=name)
            self.assertTrue(rule.is_default, name)
            self.assertEqual(rule.category, "zfs", name)


class ZfsPoolNotHealthyEvaluationTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="nas", ip_address="10.0.0.20", agent_token="tok-zfs",
            mode="managed", status=Host.Status.ONLINE)
        # Only the ZFS rule under test; the shipped CPU, memory and disk rules
        # would otherwise fire on the test host too.
        AlertRule.objects.exclude(name="ZFS pool not healthy").update(enabled=False)

    def _evaluate(self):
        with patch("apps.alerts.tasks.dispatch_alert_notification") as notify:
            _evaluate_alert_rules()
        return notify

    def test_an_unhealthy_pool_fires_the_rule(self):
        MetricPoint.objects.create(
            host=self.host, time=now() - timedelta(seconds=5),
            category="zfs", metric="pools_unhealthy", value=1)
        notify = self._evaluate()
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0].rule.name, "ZFS pool not healthy")

    def test_no_unhealthy_pools_does_not_fire_the_rule(self):
        MetricPoint.objects.create(
            host=self.host, time=now() - timedelta(seconds=5),
            category="zfs", metric="pools_unhealthy", value=0)
        notify = self._evaluate()
        self.assertEqual(notify.call_count, 0)
