"""Phase-06 licence-claim tests: `lid`, `jackil`, and `sso` meaning SAML/OIDC."""

import base64
import json
import pathlib
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.licensing.models import StoredLicense
from vigil import licensing

SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()


def _sign(claims: dict, sk: SigningKey) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    sig = sk.sign(payload).signature

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(sig)}"


def make_blob(*, instance=None, exp_delta=86400 * 90, seats=4,
              features=None, lid=None, sk=SK):
    claims = {
        "instance": instance or licensing.instance_id(),
        "org": "test-org",
        "seats": seats,
        "sites": None,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if features is not None:
        claims["features"] = features
    if lid is not None:
        claims["lid"] = lid
    return _sign(claims, sk)


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class LicenseClaimsTests(TestCase):
    def setUp(self):
        licensing.reload()

    def tearDown(self):
        StoredLicense.replace("")
        licensing.reload()

    def test_a_licence_without_a_lid_still_verifies(self):
        licensing.set_license(make_blob())
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.VALID)
        self.assertEqual(state.tier, "business")
        self.assertEqual(state.claims.lid, "")

    def test_the_lid_is_parsed_and_exposed(self):
        licensing.set_license(make_blob(lid="LIC-123"))
        self.assertEqual(licensing.current_state().claims.lid, "LIC-123")

    def test_the_licence_api_reports_the_license_id(self):
        admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(admin)
        licensing.set_license(make_blob(lid="LIC-123"))
        resp = self.client.get("/api/v1/license/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["license_id"], "LIC-123")

        StoredLicense.replace("")
        licensing.reload()
        resp = self.client.get("/api/v1/license/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["license_id"])

    def test_jackil_is_active_on_business(self):
        licensing.set_license(make_blob())
        self.assertTrue(licensing.has_feature("jackil"))

    def test_jackil_is_inactive_on_free(self):
        self.assertFalse(licensing.has_feature("jackil"))

    def test_sso_is_business_only(self):
        self.assertFalse(licensing.has_feature("sso"))
        licensing.set_license(make_blob())
        self.assertTrue(licensing.has_feature("sso"))

    def test_civil_sso_does_not_consult_the_sso_feature(self):
        # Civil SSO is free on both tiers; keep it that way.
        app_root = pathlib.Path(__file__).resolve().parent.parent / "civilsso"
        needles = ('has_feature("sso")', "has_feature('sso')",
                   'require_feature("sso")', "require_feature('sso')")
        for py in sorted(app_root.rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, text, f"{py.name} consults `sso`")
