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
