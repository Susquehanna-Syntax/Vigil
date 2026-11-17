import base64
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.hosts.models import CheckResult, DeviceCheck, Host, UnmanagedDevice
from vigil import licensing

from .models import HostUptimeSample, StatusPage

SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()


def make_blob():
    claims = {
        "instance": licensing.instance_id(), "org": "t", "seats": 4,
        "sites": None, "exp": int(time.time()) + 86400, "iat": int(time.time()),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(SK.sign(payload).signature)}"


def make_host(hostname, status=Host.Status.ONLINE):
    return Host.objects.create(hostname=hostname, status=status,
                               agent_token=uuid.uuid4().hex)


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class StatusPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_first_page_is_free_with_badge(self):
        make_host("web-01")
        make_host("db-01", status=Host.Status.OFFLINE)
        resp = self.client.post("/api/v1/status-pages/", {})
        self.assertEqual(resp.status_code, 201, resp.content)
        page = resp.json()
        self.client.patch(f"/api/v1/status-pages/{page['id']}/",
                          {"enabled": True}, content_type="application/json")
        pub = self.client_class().get(page["url"])
        self.assertEqual(pub.status_code, 200)
        self.assertIn(b"web-01", pub.content)
        self.assertIn(b"Some systems are down", pub.content)
        self.assertIn(b"Powered by", pub.content)

    def test_second_page_requires_business(self):
        self.client.post("/api/v1/status-pages/", {})
        resp = self.client.post("/api/v1/status-pages/", {})
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["feature"], "status_branding")
        licensing.set_license(make_blob())
        self.assertEqual(
            self.client.post("/api/v1/status-pages/", {}).status_code, 201)

    def test_branding_fields_require_business(self):
        page = self.client.post("/api/v1/status-pages/", {}).json()
        resp = self.client.patch(f"/api/v1/status-pages/{page['id']}/",
                                 {"hide_badge": True},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 402)
        licensing.set_license(make_blob())
        resp = self.client.patch(f"/api/v1/status-pages/{page['id']}/",
                                 {"hide_badge": True, "title": "Acme status"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["hide_badge"])

    def test_branding_falls_back_cleanly_on_lapse(self):
        licensing.set_license(make_blob())
        page = self.client.post("/api/v1/status-pages/", {}).json()
        self.client.patch(f"/api/v1/status-pages/{page['id']}/",
                          {"enabled": True, "hide_badge": True,
                           "title": "Acme status"},
                          content_type="application/json")
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()
        pub = self.client_class().get(page["url"])
        # Page still WORKS (nothing breaks on lapse); badge is back.
        self.assertEqual(pub.status_code, 200)
        self.assertIn(b"Powered by", pub.content)

    def test_disabled_or_wrong_token_is_404(self):
        page = self.client.post("/api/v1/status-pages/", {}).json()
        self.assertEqual(self.client_class().get(page["url"]).status_code, 404)
        self.assertEqual(self.client_class().get("/status/nope/").status_code, 404)

    def test_public_page_needs_no_auth(self):
        page = self.client.post("/api/v1/status-pages/", {}).json()
        self.client.patch(f"/api/v1/status-pages/{page['id']}/",
                          {"enabled": True}, content_type="application/json")
        self.assertEqual(self.client_class().get(page["url"]).status_code, 200)

    def test_admin_api_requires_admin(self):
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.get("/api/v1/status-pages/").status_code, 403)

    def test_the_responses_match_the_api_schema(self):
        from django.utils.timezone import now

        from vigil.schema_check import assert_shape

        from .api_schema import (
            StatusPageHostChoiceSerializer,
            StatusPageSerializer,
            StatusPageUptimeDaySerializer,
        )

        host = make_host("shaped-01")
        created = self.client.post("/api/v1/status-pages/", {})
        self.assertEqual(created.status_code, 201, created.content)
        assert_shape(self, created.json(), StatusPageSerializer)

        # A licensed page carries the branding keys; an unlicensed one does
        # not, which is why the serializer marks them optional.
        licensing.set_license(make_blob())
        listed = self.client.get("/api/v1/status-pages/")
        self.assertEqual(listed.status_code, 200, listed.content)
        assert_shape(self, listed.json(), StatusPageSerializer(many=True))

        hosts = self.client.get("/api/v1/status-pages/hosts/")
        self.assertEqual(hosts.status_code, 200, hosts.content)
        assert_shape(self, hosts.json(), StatusPageHostChoiceSerializer(many=True))

        HostUptimeSample.objects.create(host=host, up=True, time=now())
        uptime = self.client.get(f"/api/v1/status-pages/uptime/{host.id}/")
        self.assertEqual(uptime.status_code, 200, uptime.content)
        assert_shape(self, uptime.json(), StatusPageUptimeDaySerializer(many=True))

        patched = self.client.patch(f"/api/v1/status-pages/{created.json()['id']}/",
                                    {"title": "Shaped"}, content_type="application/json")
        self.assertEqual(patched.status_code, 200, patched.content)
        assert_shape(self, patched.json(), StatusPageSerializer)

    def test_the_refusal_matches_the_api_schema(self):
        """What an unlicensed second page answers with."""
        from vigil.schema_check import assert_shape

        from .api_schema import StatusPageUpgradeSerializer

        self.client.post("/api/v1/status-pages/", {})
        refused = self.client.post("/api/v1/status-pages/", {})
        self.assertEqual(refused.status_code, 402, refused.content)
        assert_shape(self, refused.json(), StatusPageUpgradeSerializer)


class HostLabelTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "labeladmin", password="x", is_staff=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def test_selectable_hosts_lists_non_pending(self):
        make_host("online-1")
        make_host("pending-1", status=Host.Status.PENDING)
        rows = self.client.get("/api/v1/status-pages/hosts/").json()
        names = [r["hostname"] for r in rows]
        self.assertIn("online-1", names)
        self.assertNotIn("pending-1", names)

    def test_custom_label_renders_on_public_page(self):
        h = make_host("db-internal-01")
        page = StatusPage.objects.create(
            enabled=True, host_ids=[str(h.id)],
            host_labels={str(h.id): "Primary Database"})
        pub = self.client_class().get(f"/status/{page.token}/")
        self.assertContains(pub, "Primary Database")
        self.assertNotContains(pub, "db-internal-01")

    def test_labels_saved_via_api(self):
        h = make_host("web-x")
        page = StatusPage.objects.create()
        resp = self.client.patch(f"/api/v1/status-pages/{page.id}/", {
            "host_ids": [str(h.id)], "host_labels": {str(h.id): "Public Web"}},
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["host_labels"][str(h.id)], "Public Web")


class UptimeHistoryTests(TestCase):
    def setUp(self):
        # An account exists, as on any installed Vigil: otherwise
        # SetupRedirectMiddleware sends the page to /setup/, unless an earlier
        # test in the same process already set its once-per-process flag.
        get_user_model().objects.create_user("someone", password="x")

    def test_sampler_records_one_reading_per_non_pending_host(self):
        from apps.statuspage.models import HostUptimeSample
        from apps.statuspage.tasks import sample_uptime

        up = make_host("up-1")
        make_host("down-1", status=Host.Status.OFFLINE)
        make_host("pending-1", status=Host.Status.PENDING)
        sample_uptime()
        self.assertEqual(HostUptimeSample.objects.count(), 2)  # pending skipped
        self.assertTrue(HostUptimeSample.objects.get(host=up).up)

    def test_history_buckets_by_day_and_colors_states(self):
        from datetime import timedelta

        from django.utils.timezone import now

        from apps.statuspage.models import HostUptimeSample
        from apps.statuspage.views import _uptime_history

        from datetime import datetime, time as dtime

        from django.utils.timezone import localdate, make_aware

        h = make_host("web-1")
        # Today: fully up. Yesterday: half down (degraded/down). 2 days ago: no data.
        #
        # Two samples an hour apart, not two at the same instant: the beat
        # samples on a schedule, and (host, time) is unique now — the identical
        # timestamps this used to write are the duplicate the constraint
        # exists to prevent.
        #
        # Anchored to midday of the target day rather than offset from now().
        # `now() - timedelta(days=1, hours=1)` lands in the day before last
        # whenever the clock is within an hour of midnight, which made this
        # test fail for one hour in every twenty-four — _uptime_history buckets
        # with localdate()/TruncDate, so the bucket is a calendar day, not a
        # 24-hour window.
        yesterday = localdate() - timedelta(days=1)
        yesterday_noon = make_aware(datetime.combine(yesterday, dtime(12, 0)))
        HostUptimeSample.objects.create(host=h, time=now(), up=True)
        HostUptimeSample.objects.create(host=h, time=yesterday_noon, up=True)
        HostUptimeSample.objects.create(
            host=h, time=yesterday_noon - timedelta(hours=1), up=False)
        hist = _uptime_history([h])[str(h.id)]
        self.assertEqual(len(hist["bars"]), 90)
        self.assertEqual(hist["bars"][-1]["state"], "up")        # today
        self.assertEqual(hist["bars"][-2]["state"], "down")      # 50% → down tier
        self.assertEqual(hist["bars"][-3]["state"], "nodata")    # no samples
        # 2 up of 3 total samples across the window
        self.assertAlmostEqual(float(hist["pct"]), 2 / 3)

    def test_public_page_renders_uptime_bars(self):
        from django.utils.timezone import now

        from apps.statuspage.models import HostUptimeSample

        h = make_host("web-1")
        HostUptimeSample.objects.create(host=h, time=now(), up=True)
        page = StatusPage.objects.create(enabled=True, host_ids=[str(h.id)])
        pub = self.client_class().get(f"/status/{page.token}/")
        self.assertContains(pub, "uptime-bars")
        self.assertContains(pub, "% uptime")

    def test_poll_data_endpoint_is_public_json(self):
        from django.utils.timezone import now

        from apps.statuspage.models import HostUptimeSample

        h = make_host("web-1")
        HostUptimeSample.objects.create(host=h, time=now(), up=True)
        page = StatusPage.objects.create(enabled=True, host_ids=[str(h.id)])
        # No auth — token is the access control.
        resp = self.client_class().get(f"/status/{page.token}/data/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["all_up"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["hosts"][0]["hostname"], "web-1")
        self.assertEqual(len(data["hosts"][0]["bars"]), 90)
        # A disabled page's data is 404, same as the HTML page.
        page.enabled = False
        page.save(update_fields=["enabled"])
        self.assertEqual(
            self.client_class().get(f"/status/{page.token}/data/").status_code, 404)


class HostUptimeApiTests(TestCase):
    """The dashboard's uptime widget reads the same samples the public status
    page's bars do, rather than computing availability a second way."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("up", password="pw")
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="uh", ip_address="10.95.0.2", agent_token="tk",
            status=Host.Status.ONLINE)

    def _sample(self, up, days_ago):
        from datetime import timedelta

        from django.utils.timezone import now

        HostUptimeSample.objects.create(
            host=self.host, up=up, time=now() - timedelta(days=days_ago))

    def _get(self, days=30):
        return self.client.get(
            f"/api/v1/status-pages/uptime/{self.host.id}/?days={days}")

    def test_it_answers_rather_than_erroring(self):
        """It shipped once with a NameError — the view imported nothing."""
        self.assertEqual(self._get().status_code, 200)

    def test_a_full_day_of_uptime_reads_as_one_hundred(self):
        self._sample(True, 1)
        self._sample(True, 1)
        rows = self._get().json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uptime"], 100.0)

    def test_a_half_day_down_reads_as_fifty(self):
        self._sample(True, 2)
        self._sample(False, 2)
        self.assertEqual(self._get().json()[0]["uptime"], 50.0)

    def test_samples_outside_the_window_are_not_counted(self):
        self._sample(True, 1)
        self._sample(False, 60)
        self.assertEqual(len(self._get(days=7).json()), 1)

    def test_the_window_is_clamped_to_something_sane(self):
        self._sample(True, 1)
        self.assertEqual(self._get(days=9999).status_code, 200)
        self.assertEqual(self._get(days=-5).status_code, 200)

    def test_a_host_with_no_samples_answers_empty_rather_than_failing(self):
        self.assertEqual(self._get().json(), [])

    def test_a_host_outside_your_sites_is_not_readable_by_id(self):
        """The host list is site-scoped; a per-host read that is not would let
        an operator confined to one site pull another site's history by id."""
        from unittest.mock import patch

        self._sample(True, 1)
        with patch("vigil.scoping.host_in_scope", return_value=False):
            resp = self._get()
        self.assertEqual(resp.status_code, 404)

    def test_a_host_inside_your_sites_is_readable(self):
        self._sample(True, 1)
        self.assertEqual(self._get().status_code, 200)


class PublicPageDoesNotPublishHostIdsTests(TestCase):
    """The public page keyed its cards by the host's internal id; an opaque
    per-page key does the same job (architect review, 2026-10-08)."""

    def setUp(self):
        # An account exists, as on any installed Vigil: otherwise
        # SetupRedirectMiddleware sends the page to /setup/, unless an earlier
        # test in the same process already set its once-per-process flag.
        get_user_model().objects.create_user("someone", password="x")

    def test_no_host_id_on_the_public_page_or_its_data(self):
        from apps.hosts.models import Host
        from apps.statuspage.models import StatusPage
        host = Host.objects.create(hostname="web-1", agent_token="tok-web-1-000000000000", status=Host.Status.ONLINE)
        page = StatusPage.objects.create(enabled=True, host_ids=[str(host.id)])
        for url in (f"/status/{page.token}/", f"/status/{page.token}/data/"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            body = resp.content.decode()
            self.assertIn("web-1", body)
            self.assertNotIn(str(host.id), body)


# ── Devices on a status page (S9 04c) ───────────────────────────────────────

def make_page_device(name="wiki", site_id=None):
    return UnmanagedDevice.objects.create(name=name, device_type="server",
                                          site_id=site_id)


def make_page_check(device, enabled=True):
    return DeviceCheck.objects.create(device=device, kind=DeviceCheck.Kind.HTTP,
                                      params={"url": "https://example.com/"},
                                      enabled=enabled)


def result(check, ok, when=None):
    from django.utils.timezone import now

    return CheckResult.objects.create(
        device_check=check, time=when or now(), ok=ok, detail="ok" if ok else "boom")


class DeviceRowTests(TestCase):
    """A device row on the public page is a host row: name, up or down, bars,
    uptime percent. It is up when its checks' latest results all succeeded."""

    def setUp(self):
        get_user_model().objects.create_user("someone", password="x")
        self.device = make_page_device("wiki.example")
        self.check = make_page_check(self.device)
        self.page = StatusPage.objects.create(
            enabled=True, host_ids=[], device_ids=[str(self.device.id)])

    def _data(self):
        resp = self.client_class().get(f"/status/{self.page.token}/data/")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_a_device_whose_latest_result_succeeded_shows_up_on_both_endpoints(self):
        result(self.check, True)
        data = self._data()
        self.assertEqual([h["hostname"] for h in data["hosts"]], ["wiki.example"])
        self.assertTrue(data["hosts"][0]["up"])
        self.assertTrue(data["all_up"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["hosts"][0]["bars"]), 90)
        html = self.client_class().get(f"/status/{self.page.token}/").content.decode()
        self.assertIn("wiki.example", html)

    def test_its_public_label_replaces_its_name(self):
        result(self.check, True)
        self.page.device_labels = {str(self.device.id): "Company wiki"}
        self.page.save(update_fields=["device_labels"])
        self.assertEqual(self._data()["hosts"][0]["hostname"], "Company wiki")

    def test_a_device_whose_latest_result_failed_shows_down(self):
        result(self.check, True)                    # older, then the failure
        result(self.check, False)
        data = self._data()
        self.assertFalse(data["hosts"][0]["up"])
        self.assertFalse(data["all_up"])

    def test_a_device_whose_checks_never_reported_is_not_listed(self):
        self.assertEqual(self._data()["hosts"], [])
        self.assertEqual(self._data()["total"], 0)

    def test_a_disabled_check_says_nothing_even_with_results(self):
        result(self.check, True)
        self.check.enabled = False
        self.check.save(update_fields=["enabled"])
        self.assertEqual(self._data()["hosts"], [])

    def test_empty_device_ids_lists_no_devices_though_empty_host_ids_lists_every_host(self):
        host = Host.objects.create(hostname="web-1",
                                   agent_token="tok-web-1-000000000000",
                                   status=Host.Status.ONLINE)
        page = StatusPage.objects.create(enabled=True)     # both empty
        rows = self.client_class().get(f"/status/{page.token}/data/").json()["hosts"]
        self.assertEqual([r["hostname"] for r in rows], ["web-1"])
        self.assertEqual(host.hostname, "web-1")

    def test_two_days_of_results_give_two_live_bars_with_their_percentages(self):
        from datetime import timedelta

        from django.utils.timezone import now

        result(self.check, True)
        yesterday = now() - timedelta(days=1)
        CheckResult.objects.create(device_check=self.check, time=yesterday, ok=True)
        CheckResult.objects.create(device_check=self.check,
                                   time=yesterday + timedelta(minutes=1), ok=False)
        bars = self._data()["hosts"][0]["bars"]
        today, back_one = bars[-1], bars[-2]
        self.assertEqual(today["state"], "up")
        self.assertEqual(today["pct"], 1.0)
        self.assertEqual(back_one["state"], "down")
        self.assertEqual(back_one["pct"], 0.5)
        self.assertEqual(bars[0]["state"], "nodata")

    def test_the_device_rows_come_after_the_host_rows(self):
        host = Host.objects.create(hostname="web-1",
                                   agent_token="tok-web-1-000000000000",
                                   status=Host.Status.ONLINE)
        result(self.check, True)
        self.page.host_ids = [str(host.id)]
        self.page.save(update_fields=["host_ids"])
        rows = self.client_class().get(f"/status/{self.page.token}/data/").json()["hosts"]
        self.assertEqual([r["hostname"] for r in rows], ["web-1", "wiki.example"])

    def test_the_public_page_never_publishes_another_sites_device(self):
        """A forged or stale device id saved straight on the model must not put
        a device of another site on a public page (S1 04e's rule for hosts)."""
        from django.conf import settings

        if not settings.BUSINESS_INSTALLED:
            self.skipTest("needs the vigil-business package")
        from apps_business.sites.models import Site

        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        theirs = make_page_device("zzlab-router", site_id=lab.id)
        mine = make_page_device("west-ntp", site_id=west.id)
        result(make_page_check(theirs), False)   # would show a false outage
        result(make_page_check(mine), True)
        page = StatusPage.objects.create(enabled=True, site_id=west.id,
                                         device_ids=[str(mine.id), str(theirs.id)])
        rows = self.client_class().get(f"/status/{page.token}/data/").json()["hosts"]
        self.assertEqual([r["hostname"] for r in rows], ["west-ntp"])


class DeviceEditorApiTests(TestCase):
    """/api/v1/status-pages/devices/ and the device fields of a page."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="pw")
        from apps.accounts.models import Role, UserProfile

        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.viewer = get_user_model().objects.create_user("viewer", password="pw")
        from apps.accounts.models import UserProfile as _UP

        _UP.objects.create(user=self.viewer, role=Role.VIEWER)
        self.device = make_page_device("core switch")
        self.check = make_page_check(self.device)
        self.page = StatusPage.objects.create(title="p", host_ids=[])

    def test_the_picker_answers_with_name_and_state(self):
        result(self.check, True)
        rows = self.client.get("/api/v1/status-pages/devices/").json()
        self.assertEqual([r["name"] for r in rows], ["core switch"])
        self.assertEqual(rows[0]["state"], "up")
        self.assertEqual(rows[0]["id"], str(self.device.id))

    def test_the_picker_says_null_for_a_device_no_check_reported_on(self):
        rows = self.client.get("/api/v1/status-pages/devices/").json()
        self.assertIsNone(rows[0]["state"])

    def test_a_non_admin_cannot_list_devices(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/api/v1/status-pages/devices/").status_code, 403)

    def test_an_empty_page_returns_no_devices_and_no_labels(self):
        row = self.client.get("/api/v1/status-pages/").json()[0]
        self.assertEqual(row["device_ids"], [])
        self.assertEqual(row["device_labels"], {})

    def test_saving_device_ids_and_labels_round_trips_through_the_row(self):
        resp = self.client.patch(f"/api/v1/status-pages/{self.page.id}/",
                                 data=json.dumps({
                                     "device_ids": [str(self.device.id)],
                                     "device_labels": {str(self.device.id): "Wiki"},
                                 }),
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["device_ids"], [str(self.device.id)])
        self.assertEqual(resp.json()["device_labels"], {str(self.device.id): "Wiki"})
        self.page.refresh_from_db()
        self.assertEqual(self.page.device_ids, [str(self.device.id)])

    def test_an_unknown_device_id_is_refused(self):
        resp = self.client.patch(f"/api/v1/status-pages/{self.page.id}/",
                                 data=json.dumps({"device_ids": [str(uuid.uuid4())]}),
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("device_ids", resp.json()["detail"])
        self.page.refresh_from_db()
        self.assertEqual(self.page.device_ids, [])

    def test_clearing_device_ids_removes_the_devices_and_their_labels(self):
        self.page.device_ids = [str(self.device.id)]
        self.page.device_labels = {str(self.device.id): "Wiki"}
        self.page.save()
        resp = self.client.patch(f"/api/v1/status-pages/{self.page.id}/",
                                 data=json.dumps({"device_ids": []}),
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["device_ids"], [])


class DeviceRowsQueryCountTests(TestCase):
    """The bars and states come in a fixed number of queries: a device picker
    with dozens of rows must not cost one request per device."""

    def setUp(self):
        get_user_model().objects.create_user("someone", password="x")

    def _row_queries(self, page):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = f"/status/{page.token}/data/"
        self.client.get(url)          # a cold process caches a lookup or two
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Only the device rows' own queries: a request can also write session
        # or activity rows when enough time has passed, which says nothing
        # about how many devices a page lists.
        return [q["sql"] for q in ctx.captured_queries if "hosts_" in q["sql"]]

    def test_the_data_endpoint_costs_the_same_for_two_devices_as_for_five(self):
        devices = [make_page_device(f"dev-{i}") for i in range(5)]
        for d in devices:
            result(make_page_check(d), True)
        small = StatusPage.objects.create(
            enabled=True, host_ids=[], device_ids=[str(d.id) for d in devices[:2]])
        large = StatusPage.objects.create(
            enabled=True, host_ids=[], device_ids=[str(d.id) for d in devices])
        two, five = self._row_queries(small), self._row_queries(large)
        self.assertTrue(two, "no device query was captured")
        self.assertEqual(len(two), len(five),
                         "the device rows cost queries per device, not per page")
