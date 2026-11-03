"""Install-wide guards stay install-wide once their models are scoped (S1 04c).

A uniqueness check or a licence count that runs through a scoped manager sees
only the caller's sites. These pin the ones that must count every site.
"""
import base64
import json
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.accounts.models import Role
from apps.policies.models import UpdatePolicy
from apps.statuspage.models import StatusPage
from vigil import licensing
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import Site, UserSiteRole  # noqa: E402

_KEY = SigningKey.generate()


def _license():
    claims = {"instance": licensing.instance_id(), "org": "t", "seats": 5, "sites": None,
              "exp": int(time.time()) + 86400, "iat": int(time.time()),
              "features": ["sites", "rbac_advanced"]}  # no status_branding
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(_KEY.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=base64.b64encode(_KEY.verify_key.encode()).decode())
class InstallWideGuardTests(TestCase):
    def setUp(self):
        licensing.reload()
        licensing.set_license(_license())
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.client.force_login(self.dana)

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_policy_name_taken_on_another_site_is_a_400_not_a_500(self):
        UpdatePolicy.objects.create(name="Nightly", site_id=self.lab.pk)
        self.client.raise_request_exception = False
        resp = self.client.post("/api/v1/policies/", data=json.dumps({"name": "Nightly"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("already exists", resp.json()["detail"])

    def test_free_status_page_limit_counts_every_site(self):
        StatusPage.objects.create(site_id=self.lab.pk)
        resp = self.client.post("/api/v1/status-pages/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 402, resp.content)
        self.assertEqual(StatusPage.objects.unscoped(reason="test").count(), 1)
