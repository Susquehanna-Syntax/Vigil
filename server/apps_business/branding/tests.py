"""Branding tests — licensed and unlicensed behavior per SQSY-LICENSING.md §5/§6.

Reads stay open to any authed user (the login page and every dashboard need to
theme regardless of license state); writes are Business-gated with a 402 upgrade
body, then admin-gated with a 403.
"""

import base64
import json
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from vigil import licensing

from .models import BrandingConfig

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


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class BrandingApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("op", password="x", is_staff=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def license_up(self, **kw):
        licensing.set_license(make_blob(**kw))

    # -- reads --------------------------------------------------------------

    def test_get_returns_defaults_on_fresh_install(self):
        """No row yet: load() creates it rather than 500ing."""
        self.assertFalse(BrandingConfig.objects.exists())
        resp = self.client.get("/api/v1/branding/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["product_name"], "")
        self.assertEqual(resp.json()["accent"], "")

    def test_get_is_open_without_a_license(self):
        """Reads never require a license — the UI greys and previews (§5)."""
        resp = self.client.get("/api/v1/branding/")
        self.assertEqual(resp.status_code, 200)

    def test_get_requires_authentication(self):
        self.client.logout()
        resp = self.client.get("/api/v1/branding/")
        self.assertIn(resp.status_code, (401, 403))

    # -- writes: license gate ----------------------------------------------

    def test_patch_without_license_is_402_and_changes_nothing(self):
        resp = self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({"product_name": "Acme Watch"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(BrandingConfig.load().product_name, "")

    def test_patch_with_license_is_200_and_persists(self):
        self.license_up()
        resp = self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({
                "product_name": "Acme Watch",
                "accent": "#7eddb5",
                "footer_text": "Acme Corp",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        obj = BrandingConfig.load()
        self.assertEqual(obj.product_name, "Acme Watch")
        self.assertEqual(obj.accent, "#7eddb5")
        self.assertEqual(obj.footer_text, "Acme Corp")

    # -- writes: admin gate -------------------------------------------------

    def test_patch_as_licensed_non_admin_is_403(self):
        self.license_up()
        viewer = get_user_model().objects.create_user("viewer", password="x")
        self.client.force_login(viewer)
        resp = self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({"product_name": "Nope"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(BrandingConfig.load().product_name, "")

    # -- validation ---------------------------------------------------------

    def test_invalid_accent_is_400(self):
        self.license_up()
        for bad in ("red", "#fff", "82c4ee", "#82c4e", "#gggggg"):
            resp = self.client.patch(
                "/api/v1/branding/",
                data=json.dumps({"accent": bad}),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 400, f"{bad!r} should be rejected")
        self.assertEqual(BrandingConfig.load().accent, "")

    def test_blank_accent_is_allowed(self):
        """Blank means 'use the Vigil default' — it must not trip validation."""
        self.license_up()
        resp = self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({"accent": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    # -- singleton ----------------------------------------------------------

    def test_config_is_a_singleton(self):
        self.license_up()
        self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({"product_name": "One"}),
            content_type="application/json",
        )
        self.client.patch(
            "/api/v1/branding/",
            data=json.dumps({"product_name": "Two"}),
            content_type="application/json",
        )
        self.assertEqual(BrandingConfig.objects.count(), 1)
        self.assertEqual(BrandingConfig.load().product_name, "Two")


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class LoginPageBrandingTests(TestCase):
    """Part B — the login page themes pre-auth from server context."""

    def setUp(self):
        get_user_model().objects.create_user("op", password="x", is_staff=True)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_login_page_shows_stock_identity_when_unbranded(self):
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Vigil")

    def test_login_page_shows_custom_product_name(self):
        obj = BrandingConfig.load()
        obj.product_name = "Acme Watch"
        obj.footer_text = "Acme Corp"
        obj.save()
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme Watch")
        self.assertContains(resp, "Acme Corp")
