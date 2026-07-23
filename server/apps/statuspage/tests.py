import base64
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.hosts.models import Host
from vigil import licensing

from .models import StatusPage

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

        h = make_host("web-1")
        today = now()
        # Today: fully up. Yesterday: half down (degraded/down). 2 days ago: no data.
        HostUptimeSample.objects.create(host=h, time=today, up=True)
        HostUptimeSample.objects.create(host=h, time=today - timedelta(days=1), up=True)
        HostUptimeSample.objects.create(host=h, time=today - timedelta(days=1), up=False)
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
