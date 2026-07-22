"""Sites tests — licensed and unlicensed behavior per SQSY-LICENSING.md §5/§6."""

import base64
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.hosts.models import Host
from vigil import licensing

from .models import HostSiteAssignment, Site

SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()


def make_blob(*, exp_delta=86400 * 90, seats=4):
    claims = {
        "instance": licensing.instance_id(),
        "org": "test-org",
        "seats": seats,
        "sites": None,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    sig = SK.sign(payload).signature

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(sig)}"


def make_host(hostname="h1"):
    return Host.objects.create(hostname=hostname, agent_token=f"tok-{hostname}-{uuid.uuid4()}")


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class SitesApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x", is_staff=True)
        self.client.force_login(self.user)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def license_up(self, **kw):
        licensing.set_license(make_blob(**kw))

    # -- migration ----------------------------------------------------------

    def test_default_site_exists_after_migrate(self):
        self.assertTrue(Site.objects.filter(is_default=True, slug="default").exists())

    # -- unlicensed ---------------------------------------------------------

    def test_list_is_readable_without_license(self):
        resp = self.client.get("/api/v1/sites/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Default", [s["name"] for s in resp.json()])

    def test_create_without_license_is_402_with_upgrade_body(self):
        resp = self.client.post("/api/v1/sites/", {"name": "Branch", "slug": "branch"})
        self.assertEqual(resp.status_code, 402)
        body = resp.json()
        self.assertEqual(body["feature"], "sites")
        self.assertIn("upgrade_url", body)
        self.assertEqual(Site.objects.count(), 1)

    def test_license_lapse_freezes_writes_not_reads(self):
        self.license_up(exp_delta=-30 * 86400)  # past grace
        self.assertEqual(self.client.get("/api/v1/sites/").status_code, 200)
        resp = self.client.post("/api/v1/sites/", {"name": "X", "slug": "x"})
        self.assertEqual(resp.status_code, 402)

    # -- licensed -----------------------------------------------------------

    def test_create_update_delete_with_license(self):
        self.license_up()
        resp = self.client.post("/api/v1/sites/", {"name": "Branch", "slug": "branch"})
        self.assertEqual(resp.status_code, 201, resp.content)
        sid = resp.json()["id"]
        resp = self.client.patch(f"/api/v1/sites/{sid}/", {"name": "Branch 2"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Branch 2")
        self.assertEqual(self.client.delete(f"/api/v1/sites/{sid}/").status_code, 204)
        self.assertFalse(Site.objects.filter(pk=sid).exists())

    def test_cannot_delete_default_site(self):
        self.license_up()
        default = Site.objects.get(is_default=True)
        resp = self.client.delete(f"/api/v1/sites/{default.pk}/")
        self.assertEqual(resp.status_code, 400)

    def test_unassigned_hosts_count_toward_default_site(self):
        make_host("a")
        make_host("b")
        resp = self.client.get("/api/v1/sites/")
        default = next(s for s in resp.json() if s["is_default"])
        self.assertEqual(default["host_count"], 2)

    def test_assign_and_unassign_host(self):
        self.license_up()
        site = Site.objects.create(name="Branch", slug="branch")
        host = make_host("c")
        resp = self.client.put(f"/api/v1/sites/{site.pk}/hosts/{host.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(HostSiteAssignment.objects.get(host=host).site, site)
        resp = self.client.delete(f"/api/v1/sites/{site.pk}/hosts/{host.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(HostSiteAssignment.objects.filter(host=host).exists())

    def test_delete_site_moves_hosts_to_default(self):
        self.license_up()
        site = Site.objects.create(name="Branch", slug="branch")
        host = make_host("d")
        HostSiteAssignment.objects.create(host=host, site=site)
        self.client.delete(f"/api/v1/sites/{site.pk}/")
        self.assertFalse(HostSiteAssignment.objects.filter(host=host).exists())
        resp = self.client.get("/api/v1/sites/")
        default = next(s for s in resp.json() if s["is_default"])
        self.assertEqual(default["host_count"], 1)
