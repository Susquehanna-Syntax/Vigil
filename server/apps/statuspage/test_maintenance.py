"""The public status page shows maintenance windows: a host under one reads
"maintenance", the headline says so, and upcoming windows are announced."""

import datetime as dt
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.alerts.maintenance import occurrence
from apps.alerts.models import MaintenanceWindow
from apps.hosts.models import Host

from .models import StatusPage
from .tests import make_host


def once_window(starts, ends, hosts=(), name="window", enabled=True):
    window = MaintenanceWindow.objects.create(
        name=name, kind=MaintenanceWindow.Kind.ONCE,
        starts_at=starts, ends_at=ends, enabled=enabled)
    if hosts:
        window.hosts.set(list(hosts))
    return window


class OccurrenceOnceTests(TestCase):
    def test_active_once_window_gives_its_pair(self):
        starts = now() - timedelta(hours=1)
        window = once_window(starts, now() + timedelta(hours=1))
        self.assertEqual(occurrence(window, now(), timedelta(days=7)),
                         (window.starts_at, window.ends_at))

    def test_upcoming_once_window_gives_its_pair(self):
        starts = now() + timedelta(days=2)
        window = once_window(starts, starts + timedelta(hours=2))
        self.assertEqual(occurrence(window, now(), timedelta(days=7)),
                         (window.starts_at, window.ends_at))

    def test_once_window_that_already_ended_has_none(self):
        ends = now() - timedelta(hours=1)
        window = once_window(ends - timedelta(hours=2), ends)
        self.assertIsNone(occurrence(window, now(), timedelta(days=7)))

    def test_once_window_beyond_the_horizon_has_none(self):
        starts = now() + timedelta(days=10)
        window = once_window(starts, starts + timedelta(hours=2))
        self.assertIsNone(occurrence(window, now(), timedelta(days=7)))

    def test_disabled_window_has_none(self):
        starts = now() + timedelta(days=2)
        window = once_window(starts, starts + timedelta(hours=2), enabled=False)
        self.assertIsNone(occurrence(window, now(), timedelta(days=7)))


class OccurrenceWeeklyTests(TestCase):
    # Monday 2026-11-16 12:00 UTC.
    AFTER = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)

    def weekly(self, start_time, weekdays, duration=120):
        return MaintenanceWindow.objects.create(
            name="weekly", kind=MaintenanceWindow.Kind.WEEKLY,
            weekdays=weekdays, start_time=start_time,
            duration_minutes=duration, timezone="UTC")

    def test_running_occurrence_starts_before_after(self):
        window = self.weekly(dt.time(11, 0), [self.AFTER.weekday()])
        occ = occurrence(window, self.AFTER, timedelta(days=7))
        self.assertIsNotNone(occ)
        start, end = occ
        self.assertEqual(start, dt.datetime(2026, 11, 16, 11, 0, tzinfo=dt.UTC))
        self.assertLess(start, self.AFTER)
        self.assertGreater(end, self.AFTER)

    def test_next_days_weekday_is_tomorrows_start(self):
        window = self.weekly(dt.time(3, 0), [(self.AFTER.weekday() + 1) % 7])
        occ = occurrence(window, self.AFTER, timedelta(days=7))
        self.assertEqual(occ[0], dt.datetime(2026, 11, 17, 3, 0, tzinfo=dt.UTC))

    def test_weekly_without_a_start_time_or_weekdays_has_none(self):
        window = self.weekly(dt.time(3, 0), [])
        self.assertIsNone(occurrence(window, self.AFTER, timedelta(days=7)))
        window = self.weekly(dt.time(3, 0), [self.AFTER.weekday()], duration=0)
        window.start_time = None
        self.assertIsNone(occurrence(window, self.AFTER, timedelta(days=7)))


class PublicPageMaintenanceTests(TestCase):
    def setUp(self):
        # An account exists, as on any installed Vigil: otherwise
        # SetupRedirectMiddleware answers the public page with a redirect.
        get_user_model().objects.create_user("someone", password="x")
        self.host = make_host("web-01", status=Host.Status.OFFLINE)
        self.other = make_host("not-on-page")
        self.page = StatusPage.objects.create(enabled=True,
                                              host_ids=[str(self.host.id)])
        self.public = self.client_class()

    def json(self):
        return self.public.get(f"/status/{self.page.token}/data/").json()

    def body(self):
        return self.public.get(f"/status/{self.page.token}/").content.decode()

    def test_offline_host_inside_a_window_is_maintenance_not_an_outage(self):
        once_window(now() - timedelta(hours=1), now() + timedelta(hours=1),
                    hosts=[self.host])
        data = self.json()
        self.assertEqual(data["hosts"][0]["state"], "maintenance")
        self.assertTrue(data["all_up"])
        self.assertEqual(data["maintenance_count"], 1)
        body = self.body()
        self.assertIn("Scheduled maintenance in progress", body)
        self.assertNotIn("Some systems are down",
                         body.split("<script")[0])

    def test_offline_host_without_a_window_is_down(self):
        data = self.json()
        self.assertEqual(data["hosts"][0]["state"], "down")
        self.assertFalse(data["all_up"])
        self.assertEqual(data["maintenance_count"], 0)

    def test_upcoming_window_announces_the_host_it_covers(self):
        starts = now() + timedelta(days=2)
        once_window(starts, starts + timedelta(hours=2), hosts=[self.host])
        notices = self.json()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0]["active"])
        self.assertEqual(notices[0]["hosts"], ["web-01"])
        self.assertIn("Scheduled maintenance:", self.body())

    def test_window_covering_only_a_host_off_the_page_is_not_announced(self):
        starts = now() + timedelta(days=2)
        once_window(starts, starts + timedelta(hours=2), hosts=[self.other])
        self.assertEqual(self.json()["notices"], [])
        self.assertNotIn('class="notice"', self.body())

    def test_disabled_window_is_not_announced(self):
        starts = now() + timedelta(days=2)
        once_window(starts, starts + timedelta(hours=2), hosts=[self.host],
                    enabled=False)
        self.assertEqual(self.json()["notices"], [])

    def test_window_name_never_reaches_the_public_page(self):
        starts = now() + timedelta(days=2)
        once_window(starts, starts + timedelta(hours=2), hosts=[self.host],
                    name="secret-internal-patch-name")
        body = self.body()
        self.assertIn("Scheduled maintenance:", body.split("<script")[0])
        self.assertNotIn("secret-internal-patch-name", body)
        self.assertNotIn("secret-internal-patch-name",
                         self.public.get(f"/status/{self.page.token}/data/"
                                         ).content.decode())

    def test_maintenance_until_is_maintenance_without_a_notice(self):
        self.host.maintenance_until = now() + timedelta(hours=1)
        self.host.save(update_fields=["maintenance_until"])
        data = self.json()
        self.assertEqual(data["hosts"][0]["state"], "maintenance")
        self.assertTrue(data["all_up"])
        self.assertEqual(data["notices"], [])

    def test_json_notices_carry_nothing_but_the_public_fields(self):
        starts = now() + timedelta(days=2)
        once_window(starts, starts + timedelta(hours=2), hosts=[self.host])
        notice = self.json()["notices"][0]
        self.assertEqual(
            sorted(notice), ["active", "ends_at", "hosts", "starts_at"])
        self.assertEqual(notice["starts_at"], starts.isoformat())


class PublicPageWithoutMaintenanceTests(TestCase):
    """Nothing any window touches: the page says nothing about maintenance."""

    def setUp(self):
        get_user_model().objects.create_user("someone", password="x")
        self.host = make_host("web-01")
        self.page = StatusPage.objects.create(enabled=True,
                                              host_ids=[str(self.host.id)])
        self.public = self.client_class()

    def test_all_up_and_no_notice_markup_at_all(self):
        body = self.public.get(f"/status/{self.page.token}/").content.decode()
        head = body.split("<script")[0]
        data = self.public.get(f"/status/{self.page.token}/data/").json()
        self.assertIn("All systems operational", head)
        self.assertEqual(data["maintenance_count"], 0)
        self.assertEqual(data["notices"], [])
        self.assertNotIn('class="notice"', head)
