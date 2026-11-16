"""Bucketed history on the metric history endpoint.

A range longer than 24 hours answers with one point per hour (up to 30 days)
or per day (longer), each carrying the average, minimum and maximum of its
bucket, so a chart can draw a band and spikes still show. Ranges of 24 hours
or less keep stride-sampling raw rows.

On PostgreSQL with TimescaleDB the buckets come from the continuous
aggregates; elsewhere they are computed from raw rows with the ORM. These run
on SQLite unless a test says otherwise, so ``has_rollups()`` is False and
every bucket comes from raw rows: test 9 patches the PostgreSQL split in, and
test 10 reads the real views where the database has them.

Every range ends at ``NOW``, passed as ``to=``, so no bucket edge moves while
a test runs. It is the current minute rather than a fixed date because the
history endpoint answers relative to the real clock when ``to`` is left out.
"""

from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.metrics import rollups
from apps.metrics.models import MetricPoint

#: Where every range below ends: the current minute exactly, so the microseconds
#: a query sees are the ones the data was written with.
NOW = now().replace(second=0, microsecond=0)


def _iso(moment):
    """``moment`` as ISO-8601 with a ``Z`` suffix."""
    return moment.isoformat().replace("+00:00", "Z")


class PickBucketTests(TestCase):
    def test_boundaries_between_raw_hourly_and_daily(self):
        skew = rollups.SKEW
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(hours=24), NOW), "raw")
        # The 24h and 30d buttons measure from the browser's clock: a request
        # arriving a moment (or a few minutes) later keeps its resolution.
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(hours=24, seconds=3), NOW), "raw")
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(hours=24) - skew, NOW), "raw")
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(hours=24, seconds=1) - skew, NOW), "1h")
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(days=30, seconds=3), NOW), "1h")
        self.assertEqual(rollups.pick_bucket(NOW - timedelta(days=30, seconds=1) - skew, NOW), "1d")


class BucketedHistoryTests(TestCase):
    def setUp(self):
        rollups._views_present = None
        user = get_user_model().objects.create_user("m", password="x")
        self.client.force_login(user)
        self.host = Host.objects.create(hostname="m1", ip_address="10.42.0.1",
                                        agent_token="m1")

    def tearDown(self):
        rollups._views_present = None

    def _add(self, moment, value, labels=None):
        MetricPoint.objects.create(host=self.host, category="system", metric="cpu",
                                   time=moment, value=value, labels=labels or {})

    def _get(self, **params):
        r = self.client.get(
            f"/api/v1/metrics/{self.host.id}/system/cpu/?{urlencode(params)}")
        self.assertEqual(r.status_code, 200)
        return r

    def test_three_day_range_answers_hourly_with_avg_min_max(self):
        hour = rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=1)
        for value in (10, 50, 30):
            self._add(hour + timedelta(minutes=7), float(value))
        r = self._get(**{"from": _iso(rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=3)),
                         "to": _iso(NOW)})
        self.assertEqual(r["X-Vigil-Bucket"], "1h")
        rows = [row for row in r.json() if row["time"].startswith(hour.strftime("%Y-%m-%dT%H"))]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 30.0)
        self.assertEqual(rows[0]["min"], 10.0)
        self.assertEqual(rows[0]["max"], 50.0)

    def test_bucketed_rows_come_back_newest_first(self):
        base = rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=2)
        for offset in (0, 3600, 7200):
            self._add(base + timedelta(seconds=offset), float(offset))
        r = self._get(**{"from": _iso(base - timedelta(days=1)), "to": _iso(NOW)})
        times = [row["time"] for row in r.json()]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_sixty_day_range_answers_daily_with_avg_min_max(self):
        day = rollups._floor(NOW, timedelta(days=1)) - timedelta(days=10)
        for i, value in enumerate((5.0, 15.0, 25.0)):
            self._add(day + timedelta(hours=3, minutes=i), value)
        r = self._get(**{"from": _iso(day - timedelta(days=60)), "to": _iso(NOW)})
        self.assertEqual(r["X-Vigil-Bucket"], "1d")
        rows = [row for row in r.json() if row["time"].startswith(day.strftime("%Y-%m-%d"))]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 15.0)
        self.assertEqual(rows[0]["min"], 5.0)
        self.assertEqual(rows[0]["max"], 25.0)

    def test_distinct_label_sets_stay_separate_rows_in_one_hour(self):
        hour = rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=1)
        self._add(hour + timedelta(minutes=5), 10.0, {"interface": "eth0"})
        self._add(hour + timedelta(minutes=25), 40.0, {"interface": "eth1"})
        r = self._get(**{"from": _iso(rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=3)),
                         "to": _iso(NOW)})
        rows = [row for row in r.json() if row["time"].startswith(hour.strftime("%Y-%m-%dT%H"))]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["value"] for row in rows), [10.0, 40.0])

    def test_twelve_hour_range_keeps_stride_sampling_over_the_limit(self):
        oldest = NOW - timedelta(hours=12)
        MetricPoint.objects.bulk_create([
            MetricPoint(host=self.host, category="system", metric="cpu",
                        time=oldest + timedelta(minutes=i), value=float(i))
            for i in range(700)
        ])
        r = self._get(**{"from": _iso(oldest), "to": _iso(NOW), "limit": 100})
        self.assertEqual(r["X-Vigil-Bucket"], "raw")
        self.assertEqual(r["X-Vigil-Sampled"], "1")
        rows = r.json()
        self.assertLessEqual(len(rows), 101)
        self.assertGreater(len(rows), 1)
        self.assertNotIn("min", rows[0])

    def test_request_without_from_is_raw(self):
        self._add(NOW - timedelta(minutes=5), 1.0)
        r = self._get()
        self.assertEqual(r["X-Vigil-Bucket"], "raw")
        self.assertEqual(len(r.json()), 1)

    def test_point_at_to_is_excluded_and_one_at_the_floored_start_included(self):
        """The bucket window opens at the whole hour at or before ``from``, so a
        point there counts even though it predates ``from``; and it closes
        before ``to``, so a point exactly there does not."""
        start = rollups._floor(NOW, timedelta(hours=1)) - timedelta(days=2)
        self._add(start, 7.0)
        self._add(NOW, 9.0)
        rows = self._get(**{"from": _iso(start + timedelta(minutes=30)),
                            "to": _iso(NOW)}).json()
        self.assertTrue(rows, "the point at the floored start was dropped")
        self.assertEqual({row["value"] for row in rows}, {7.0})

    def test_a_from_without_an_offset_is_read_in_the_time_zone(self):
        """A naive ``from`` used to reach the bucket arithmetic unconverted
        and fail with a 500."""
        from django.utils import timezone
        self._add(NOW - timedelta(days=1), 3.0)
        naive = timezone.make_naive(NOW - timedelta(days=3)).isoformat()
        r = self._get(**{"from": naive, "to": _iso(NOW)})
        self.assertEqual(r["X-Vigil-Bucket"], "1h")
        self.assertEqual([row["value"] for row in r.json()], [3.0])

    def test_an_answer_stops_at_the_row_cap_keeping_the_newest(self):
        day = rollups._floor(NOW, timedelta(days=1))
        for i in range(5):
            self._add(day - timedelta(days=i + 1), float(i))
        with patch.object(rollups, "MAX_ROWS", 3):
            r = self._get(**{"from": _iso(NOW - timedelta(days=60)), "to": _iso(NOW)})
        self.assertEqual([row["value"] for row in r.json()], [0.0, 1.0, 2.0])
        self.assertEqual(r["X-Vigil-Truncated"], "1")

    def test_has_rollups_is_false_on_sqlite(self):
        self.assertFalse(rollups.has_rollups())

    def test_history_mixes_raw_newest_part_with_the_view_when_present(self):
        start = NOW - timedelta(days=10)
        size, lag = rollups.BUCKETS["1h"][1], rollups.BUCKETS["1h"][2]
        boundary = rollups._floor(NOW - lag, size)
        marker = {"time": start, "value": 1.0, "min": 1.0, "max": 1.0, "labels": {}}
        self._add(boundary + timedelta(minutes=30), 42.0)
        raw = rollups._from_raw(self.host.pk, "system", "cpu", boundary, NOW, size)
        self.assertTrue(raw, "no raw point fell in the newest lag window")

        with patch.object(rollups, "has_rollups", return_value=True), \
                patch.object(rollups, "_from_view", return_value=[marker]) as view:
            rows = rollups.history(self.host.pk, "system", "cpu", start, NOW, "1h")

        view.assert_called_once_with(rollups.BUCKETS["1h"][0], self.host.pk,
                                     "system", "cpu", rollups._floor(start, size), boundary,
                                     rollups.MAX_ROWS - len(raw))
        self.assertEqual(rows[:len(raw)], raw)
        self.assertEqual(rows[len(raw):], [marker])

    @skipUnless(connection.vendor == "postgresql",
                "the continuous aggregates exist only on PostgreSQL with TimescaleDB")
    def test_from_view_reads_both_real_views(self):
        start = NOW - timedelta(days=10)
        for view, _size, _lag in rollups.BUCKETS.values():
            self.assertEqual(
                rollups._from_view(view, self.host.pk, "system", "cpu", start, NOW), [])
