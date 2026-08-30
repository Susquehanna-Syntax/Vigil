"""Time-range sampling on the metric history endpoint.

The bug: `qs[:limit]` over a newest-first queryset truncated from the *newest*
end, so a 7-day request over minute-resolution data returned the most recent
500 points — about eight hours. The 24h and 7d buttons rendered near-identical
charts and looked inert.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint


class DownsamplingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("m", password="x")
        self.client.force_login(self.user)
        self.host = Host.objects.create(hostname="m1", ip_address="10.42.0.1",
                                        agent_token="m1")
        # 3000 points spread evenly over seven days — enough to exceed the
        # 1000-point cap, and dense enough that a 24h window still has data.
        self.oldest = now() - timedelta(days=7)
        step = timedelta(days=7) / 3000
        MetricPoint.objects.bulk_create([
            MetricPoint(host=self.host, category="system", metric="cpu",
                        time=self.oldest + step * i, value=i % 100)
            for i in range(3000)
        ])
        self.all_of_it = (now() - timedelta(days=8)).isoformat()

    def _get(self, **params):
        qs = urlencode(params)
        return self.client.get(
            f"/api/v1/metrics/{self.host.id}/system/cpu/?{qs}")

    def test_a_wide_window_spans_the_whole_window(self):
        """The regression: the returned series must cover the range asked for,
        not just its newest tail."""
        r = self._get(**{"from": self.all_of_it, "limit": 300})
        points = r.json()
        self.assertGreater(len(points), 0)
        from django.utils.dateparse import parse_datetime
        times = sorted(parse_datetime(p["time"]) for p in points)
        span = times[-1] - times[0]
        self.assertGreater(span, timedelta(days=1),
                           "a 7-day request returned less than a day of data — "
                           "it is truncating instead of sampling")

    def test_the_limit_is_respected(self):
        r = self._get(**{"from": self.all_of_it, "limit": 300})
        self.assertLessEqual(len(r.json()), 302)   # +newest-point insertion

    def test_the_newest_point_is_always_included(self):
        """A chart whose right edge lags looks stale even when it is current."""
        r = self._get(**{"from": self.all_of_it, "limit": 50})
        newest_in_db = MetricPoint.objects.filter(
            host=self.host, category="system", metric="cpu").order_by("-time").first()
        returned = {p["time"] for p in r.json()}
        self.assertIn(newest_in_db.time.isoformat().replace("+00:00", "Z"),
                      {t.replace("+00:00", "Z") for t in returned})

    def test_a_narrow_window_is_not_sampled(self):
        r = self._get(**{"from": (now() - timedelta(hours=2)).isoformat(),
                         "limit": 1000})
        self.assertEqual(r["X-Vigil-Sampled"], "0")

    def test_sampling_is_flagged_in_the_response(self):
        r = self._get(**{"from": self.all_of_it, "limit": 100})
        self.assertEqual(r["X-Vigil-Sampled"], "1")
        self.assertEqual(r["X-Vigil-Total-Points"], "3000")

    def test_different_ranges_return_different_spans(self):
        """The user-visible symptom: 24h and 7d looked identical."""
        from django.utils.dateparse import parse_datetime

        def span_for(days):
            since = (now() - timedelta(days=days)).isoformat()
            pts = self._get(**{"from": since, "limit": 300}).json()
            self.assertTrue(pts, f"no data at all for a {days}-day window")
            times = sorted(parse_datetime(p["time"]) for p in pts)
            return times[-1] - times[0]

        self.assertGreater(span_for(7), span_for(1) + timedelta(hours=12),
                           "7d and 24h returned near-identical spans")
