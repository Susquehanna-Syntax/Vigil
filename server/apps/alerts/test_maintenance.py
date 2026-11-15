"""Maintenance windows: the logic (apps.alerts.maintenance) and its API."""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.metrics.models import MetricPoint
from vigil import scoping
from vigil.testsupport import requires_business

from .maintenance import MAX_WEEKLY_MINUTES, covers, is_active
from .models import Alert, AlertRule, MaintenanceWindow, NotificationChannel
from .tasks import evaluate_alert_rules

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _host(name="box", **kw):
    return Host.objects.create(
        hostname=name, ip_address=f"10.9.{len(name) % 200}.7",
        agent_token=f"tok-{name}" + "0" * 24,
        mode="managed", status=Host.Status.ONLINE, **kw)


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="pw")
    UserProfile.objects.create(user=user, role=role)
    return user


def _once(name="Patch night", **extra):
    start = now() + timedelta(days=1)
    body = {"name": name, "kind": "once",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=2)).isoformat()}
    body.update(extra)
    return body


class OneOffActiveTests(TestCase):
    def test_the_window_is_open_from_its_start_until_its_end(self):
        start = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
        window = MaintenanceWindow.objects.create(
            name="Patch night", kind="once", starts_at=start, ends_at=start + timedelta(hours=3))
        self.assertFalse(is_active(window, start - timedelta(seconds=1)))
        self.assertTrue(is_active(window, start))
        self.assertFalse(is_active(window, start + timedelta(hours=3)))

    def test_a_disabled_window_is_never_active(self):
        start = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
        window = MaintenanceWindow.objects.create(
            name="Patch night", kind="once", starts_at=start,
            ends_at=start + timedelta(hours=3), enabled=False)
        self.assertFalse(is_active(window, start + timedelta(hours=1)))


class WeeklyActiveTests(TestCase):
    def _window(self, **kw):
        defaults = {"name": "Nightly", "kind": "weekly", "weekdays": [0],
                    "start_time": datetime(2026, 5, 4, 22, 0, tzinfo=NY).time(),
                    "duration_minutes": 180, "timezone": "America/New_York"}
        defaults.update(kw)
        return MaintenanceWindow.objects.create(**defaults)

    def test_a_monday_window_covers_the_night_into_tuesday(self):
        window = self._window()
        self.assertTrue(is_active(window, datetime(2026, 5, 4, 23, 30, tzinfo=NY)))
        self.assertTrue(is_active(window, datetime(2026, 5, 5, 0, 30, tzinfo=NY)))
        self.assertFalse(is_active(window, datetime(2026, 5, 5, 1, 0, tzinfo=NY)))

    def test_another_weekday_is_not_active(self):
        window = self._window()
        self.assertFalse(is_active(window, datetime(2026, 5, 6, 23, 0, tzinfo=NY)))

    def test_a_window_across_the_dst_change_answers_by_local_time(self):
        """2026-03-08 is the spring-forward Sunday in New York: 02:00 local
        never happens. The window is still judged by the wall clock."""
        window = self._window(
            weekdays=[6],
            start_time=datetime(2026, 3, 8, 1, 30, tzinfo=NY).time(),
            duration_minutes=60)
        self.assertTrue(is_active(window, datetime(2026, 3, 8, 1, 45, tzinfo=NY)))
        # 03:15 local is already past the hour that began at 01:30 local.
        self.assertFalse(is_active(window, datetime(2026, 3, 8, 3, 15, tzinfo=NY)))

    def test_the_duration_is_clamped_to_a_day(self):
        window = self._window(duration_minutes=MAX_WEEKLY_MINUTES * 2)
        self.assertTrue(is_active(window, datetime(2026, 5, 4, 23, 30, tzinfo=NY)))


class CoversTests(TestCase):
    def test_a_window_with_no_hosts_and_no_tags_covers_every_host(self):
        window = MaintenanceWindow.objects.create(name="Everything", kind="once")
        self.assertTrue(covers(window, _host()))

    def test_a_named_host_covers_only_that_host(self):
        kept = _host("kept")
        other = _host("other")
        window = MaintenanceWindow.objects.create(name="One box", kind="once")
        window.hosts.add(kept)
        self.assertTrue(covers(window, kept))
        self.assertFalse(covers(window, other))

    def test_a_tag_matches_case_insensitively(self):
        host = _host("tagged", tags=["DB-Primary"])
        window = MaintenanceWindow.objects.create(name="Databases", kind="once", tags=["db-primary"])
        self.assertTrue(covers(window, host))

    def test_a_host_outside_the_windows_site_is_not_covered(self):
        if scoping._sites_models() is None:
            self.skipTest("the Business sites app is not installed")
        from apps_business.sites.models import HostSiteAssignment, Site

        site = Site.objects.create(name="West", slug="west")
        covered = _host("west-box")
        HostSiteAssignment.objects.create(host=covered, site=site)
        window = MaintenanceWindow.objects.create(name="Elsewhere", kind="once", site_id=site.pk)
        self.assertFalse(covers(window, _host("somewhere-else")))
        self.assertTrue(covers(window, covered))


class MetricAlertsSkipWindowsTests(TestCase):
    def setUp(self):
        self.host = _host("breach")
        # Vigil ships 20 default rules in migrations; silence them so this
        # measures only the rule below.
        AlertRule.objects.update(enabled=False)
        AlertRule.objects.create(
            name="Busy disk", category="disk", metric="usage_percent",
            operator="gt", threshold=90.0, severity="warning", enabled=True,
            duration_seconds=0)
        MetricPoint.objects.create(
            host=self.host, time=now(), category="disk",
            metric="usage_percent", value=97.0)

    def _window(self):
        return MaintenanceWindow.objects.create(
            name="Night", kind="once", starts_at=now() - timedelta(minutes=5),
            ends_at=now() + timedelta(minutes=30))

    def test_a_host_in_a_window_raises_no_alert_and_one_after_it_ends(self):
        window = self._window()
        with patch("apps.alerts.tasks.dispatch_alert_notification") as notify:
            evaluate_alert_rules()
        self.assertEqual(Alert.objects.count(), 0)
        self.assertEqual(notify.call_count, 0)

        window.ends_at = now() - timedelta(minutes=1)
        window.save()
        with patch("apps.alerts.tasks.dispatch_alert_notification") as notify:
            evaluate_alert_rules()
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(notify.call_count, 1)


class NotificationSuppressionTests(TestCase):
    def setUp(self):
        self.host = _host("paged")
        NotificationChannel.objects.create(
            name="pager", kind="generic_webhook", config={"url": "https://example.test/hook"})
        self.alert = Alert.objects.create(host=self.host, severity="warning", message="disk full")
        self.window = MaintenanceWindow.objects.create(
            name="Night", kind="once", starts_at=now() - timedelta(minutes=5),
            ends_at=now() + timedelta(minutes=30))

    def test_nothing_is_delivered_while_the_window_is_open(self):
        from . import notifications

        with patch("apps.alerts.notifications.deliver") as deliver:
            notifications.dispatch_alert_notification(self.alert, "firing")
        deliver.assert_not_called()

    def test_a_disabled_window_still_pages(self):
        from . import notifications

        self.window.enabled = False
        self.window.save()
        with patch("apps.alerts.notifications.deliver") as deliver:
            notifications.dispatch_alert_notification(self.alert, "firing")
        deliver.assert_called()


INDEX = "/api/v1/alerts/maintenance/"


def _detail(window_id):
    return f"/api/v1/alerts/maintenance/{window_id}/"


class MaintenanceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = _user("admin", Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_an_admin_creates_a_one_off_and_a_weekly_window(self):
        created = self.client.post(INDEX, _once(), format="json")
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(body["kind"], "once")
        self.assertFalse(body["active"])

        weekly = self.client.post(INDEX, {
            "name": "Weekly rebuild", "kind": "weekly", "weekdays": [1, 3],
            "start_time": "02:00", "duration_minutes": 90, "timezone": "Europe/Berlin",
        }, format="json")
        self.assertEqual(weekly.status_code, 201, weekly.content)
        body = weekly.json()
        self.assertEqual(body["weekdays"], [1, 3])
        self.assertEqual(body["start_time"], "02:00")
        self.assertEqual(body["duration_minutes"], 90)

    def test_name_is_required_and_bounded(self):
        self.assertEqual(
            self.client.post(INDEX, _once(name=""), format="json").status_code, 400)
        answer = self.client.post(INDEX, _once(name="x" * 201), format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertIn("error", answer.json())

    def test_kind_must_be_once_or_weekly(self):
        answer = self.client.post(INDEX, {"name": "w", "kind": "monthly"}, format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertIn("kind", answer.json()["error"])

    def test_a_one_off_needs_both_ends_as_aware_datetimes(self):
        start = now()
        cases = [
            {"name": "w", "kind": "once"},
            {"name": "w", "kind": "once", "starts_at": start.isoformat()},
            {"name": "w", "kind": "once", "starts_at": "not a date",
             "ends_at": (start + timedelta(hours=1)).isoformat()},
            # naive: no offset at all
            {"name": "w", "kind": "once", "starts_at": start.replace(tzinfo=None).isoformat(),
             "ends_at": (start + timedelta(hours=1)).isoformat()},
            {"name": "w", "kind": "once", "starts_at": (start + timedelta(hours=1)).isoformat(),
             "ends_at": start.isoformat()},
        ]
        for body in cases:
            answer = self.client.post(INDEX, body, format="json")
            self.assertEqual(answer.status_code, 400, body)
            self.assertIn("error", answer.json())

    def test_weekly_fields_are_checked(self):
        base = {"name": "w", "kind": "weekly", "weekdays": [1],
                "start_time": "02:00", "duration_minutes": 90, "timezone": "UTC"}
        bad = [
            dict(base, weekdays=[]),
            dict(base, weekdays="mondays"),
            dict(base, weekdays=[3, 3]),
            dict(base, weekdays=[7]),
            dict(base, start_time="2am"),
            dict(base, start_time="25:00"),
            dict(base, duration_minutes=0),
            dict(base, duration_minutes=MAX_WEEKLY_MINUTES + 1),
            dict(base, timezone="Mars/Olympus"),
        ]
        for body in bad:
            answer = self.client.post(INDEX, body, format="json")
            self.assertEqual(answer.status_code, 400, body)
            self.assertIn("error", answer.json())

    def test_host_ids_must_name_hosts_the_user_can_see(self):
        known = _host("visible-box")
        answer = self.client.post(INDEX, _once(host_ids=[
            str(known.id), "11111111-1111-1111-1111-111111111111"]), format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertIn("Unknown host", answer.json()["error"])

    def test_tags_must_be_short_strings(self):
        self.assertEqual(
            self.client.post(INDEX, _once(tags="db"), format="json").status_code, 400)
        self.assertEqual(
            self.client.post(INDEX, _once(tags=["x" * 101]), format="json").status_code, 400)
        ok = self.client.post(INDEX, _once(tags=["db"]), format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["tags"], ["db"])

    def test_patch_renames_and_delete_answers_204(self):
        created = self.client.post(INDEX, _once(name="Old name"), format="json").json()
        renamed = self.client.patch(_detail(created["id"]), {"name": "New name"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        self.assertEqual(renamed.json()["name"], "New name")
        self.assertEqual(renamed.json()["kind"], "once")

        self.assertEqual(self.client.delete(_detail(created["id"])).status_code, 204)
        self.assertEqual(self.client.get(_detail(created["id"])).status_code, 404)

    def test_a_non_admin_is_refused(self):
        viewer = _user("watcher", Role.VIEWER)
        self.client.force_authenticate(viewer)
        self.assertEqual(self.client.get(INDEX).status_code, 403)
        self.assertEqual(self.client.post(INDEX, _once(), format="json").status_code, 403)


@requires_business
class MaintenanceApiSiteTests(TestCase):
    def setUp(self):
        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole

        self.client = APIClient()
        self.admin = _user("root", Role.ADMIN)

        self.site_a = Site.objects.create(name="Alpha", slug="alpha")
        self.site_b = Site.objects.create(name="Bravo", slug="bravo")
        self.host_b = _host("bravo-box")
        HostSiteAssignment.objects.create(host=self.host_b, site=self.site_b)

        self.anna = get_user_model().objects.create_user("anna", password="pw")
        UserProfile.objects.create(user=self.anna, role=Role.VIEWER)
        UserSiteRole.objects.create(user=self.anna, site=self.site_a, role="admin")

    def _window(self, site):
        return MaintenanceWindow.objects.create(
            name=f"{site.name} window", site_id=site.pk, kind="once",
            starts_at=now(), ends_at=now() + timedelta(hours=1))

    def test_a_site_admin_cannot_reach_another_sites_window(self):
        self.client.force_authenticate(self.anna)
        foreign = self._window(self.site_b)
        self.assertEqual(self.client.get(INDEX).json(), [])
        self.assertEqual(self.client.get(_detail(foreign.id)).status_code, 404)
        self.assertEqual(
            self.client.patch(_detail(foreign.id), {"name": "mine"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(_detail(foreign.id)).status_code, 404)

    def test_naming_another_sites_host_answers_unknown_host(self):
        self.client.force_authenticate(self.anna)
        answer = self.client.post(INDEX, _once(
            site_id=str(self.site_a.pk), host_ids=[str(self.host_b.id)]), format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertIn("Unknown host", answer.json()["error"])

    def test_a_site_admin_cannot_create_a_global_window(self):
        self.client.force_authenticate(self.anna)
        answer = self.client.post(INDEX, _once(name="Everywhere"), format="json")
        self.assertEqual(answer.status_code, 403)

    def test_an_instance_admin_creates_a_global_window(self):
        self.client.force_authenticate(self.admin)
        answer = self.client.post(INDEX, _once(name="Everywhere"), format="json")
        self.assertEqual(answer.status_code, 201, answer.content)
        self.assertIsNone(answer.json()["site_id"])
