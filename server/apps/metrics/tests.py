from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint


class MetricHistoryParamGuardTests(TestCase):
    """Untrusted query params must 400, never 500."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32, status=Host.Status.ONLINE,
        )
        self.base = f"/api/v1/metrics/{self.host.id}/cpu/usage/"

    def test_valid_request_ok(self):
        self.assertEqual(self.client.get(self.base).status_code, 200)

    def test_non_numeric_limit_is_400(self):
        self.assertEqual(self.client.get(self.base + "?limit=abc").status_code, 400)

    def test_malformed_timestamp_is_400(self):
        self.assertEqual(self.client.get(self.base + "?from=notadate").status_code, 400)

    def test_limit_is_capped(self):
        # Over-cap limit shouldn't error; it's clamped server-side.
        self.assertEqual(self.client.get(self.base + "?limit=999999").status_code, 200)


class MetricCatalogTests(TestCase):
    """The picker may only offer metrics something actually reports."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("cat", password="pw")
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h", ip_address="10.90.0.2", agent_token="t",
            status=Host.Status.ONLINE)

    def _point(self, category, metric, ago_hours=1):
        from datetime import timedelta

        from django.utils.timezone import now

        MetricPoint.objects.create(
            host=self.host, category=category, metric=metric, value=1.0,
            time=now() - timedelta(hours=ago_hours))

    def test_it_lists_the_pairs_being_reported(self):
        self._point("cpu", "usage_percent")
        self._point("memory", "usage_percent")
        rows = self.client.get("/api/v1/metrics/catalog/").json()
        pairs = {(r["category"], r["metric"]) for r in rows}
        self.assertEqual(pairs, {("cpu", "usage_percent"), ("memory", "usage_percent")})

    def test_each_pair_appears_once_however_many_points_exist(self):
        for _ in range(5):
            self._point("cpu", "usage_percent")
        rows = self.client.get("/api/v1/metrics/catalog/").json()
        self.assertEqual(len(rows), 1)

    def test_a_metric_nothing_has_reported_lately_is_not_offered(self):
        self._point("cpu", "usage_percent", ago_hours=1)
        self._point("legacy", "old_metric", ago_hours=200)
        rows = self.client.get("/api/v1/metrics/catalog/").json()
        self.assertNotIn("legacy", {r["category"] for r in rows})

    def test_it_carries_a_label_the_picker_can_show(self):
        self._point("cpu", "usage_percent")
        row = self.client.get("/api/v1/metrics/catalog/").json()[0]
        self.assertEqual(row["label"], "cpu / usage_percent")

    def test_an_idle_fleet_still_gets_a_usable_picker(self):
        """No recent points at all — a fresh install, or every agent offline."""
        from apps.metrics.views import COLLECTOR_METRICS

        MetricPoint.objects.all().delete()
        rows = self.client.get("/api/v1/metrics/catalog/").json()
        self.assertEqual(len(rows), len(COLLECTOR_METRICS))
        self.assertIn(("cpu", "usage_percent"),
                      {(r["category"], r["metric"]) for r in rows})

    def test_live_data_takes_precedence_over_the_fallback(self):
        self._point("custom", "my_metric")
        rows = self.client.get("/api/v1/metrics/catalog/").json()
        self.assertEqual([(r["category"], r["metric"]) for r in rows],
                         [("custom", "my_metric")])
