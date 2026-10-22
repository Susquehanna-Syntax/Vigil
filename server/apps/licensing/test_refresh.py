"""Anti-piracy: which key a build trusts, and refreshing a license from Mercantil."""
import base64
import tempfile
import time
from pathlib import Path
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import SimpleTestCase, TestCase, override_settings
from nacl.signing import VerifyKey
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.licensing.models import InstallKey, StoredLicense
from vigil import licensing
from vigil.license_keys import (
    DEV_PUBLIC_KEY,
    PRODUCTION_PUBLIC_KEY,
    RELEASE_MARKER,
    trusted_public_key,
)

from .tests import OTHER_SK, PUB, make_blob

ATTACKER_KEY = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY="


class TrustedKeyTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _release(self):
        (self.dir / RELEASE_MARKER).touch()

    def test_a_release_build_ignores_a_key_from_the_environment(self):
        self._release()
        for debug in (False, True):
            with self.subTest(debug=debug):
                key = trusted_public_key(self.dir, debug=debug,
                                         environ={"VIGIL_LICENSE_PUBLIC_KEY": ATTACKER_KEY})
                self.assertEqual(key, PRODUCTION_PUBLIC_KEY)

    def test_a_release_build_can_only_switch_to_the_built_in_dev_key(self):
        self._release()
        key = trusted_public_key(self.dir, debug=False, environ={
            "VIGIL_ACCEPT_DEV_LICENSES": "true", "VIGIL_LICENSE_PUBLIC_KEY": ATTACKER_KEY})
        self.assertEqual(key, DEV_PUBLIC_KEY)

    def test_a_source_checkout_keeps_the_development_behaviour(self):
        self.assertEqual(trusted_public_key(self.dir, debug=True, environ={}), DEV_PUBLIC_KEY)
        self.assertEqual(trusted_public_key(self.dir, debug=False, environ={}), "")
        self.assertEqual(trusted_public_key(
            self.dir, debug=False, environ={"VIGIL_LICENSE_PUBLIC_KEY": ATTACKER_KEY}), ATTACKER_KEY)

    def test_the_override_is_called_out_on_a_release_build(self):
        self._release()
        with override_settings(BASE_DIR=self.dir), \
                mock.patch.dict("os.environ", {"VIGIL_LICENSE_PUBLIC_KEY": ATTACKER_KEY}):
            ids = [m.id for m in run_checks()]
        self.assertIn("licensing.W001", ids)


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB, VIGIL_LICENSE_REFRESH=True,
                   VIGIL_LICENSE_SERVER="https://license.example")
class RefreshTests(TestCase):
    def setUp(self):
        licensing.reload()

    def tearDown(self):
        StoredLicense.replace("")
        licensing.reload()

    def _apply(self, **kw):
        licensing.set_license(make_blob(**kw))

    def _answer(self, response):
        if isinstance(response, Exception):
            return mock.patch("requests.get", side_effect=response)
        return mock.patch("requests.get", return_value=response)

    def test_a_free_install_never_calls_out(self):
        with self._answer(FakeResponse(200, {"license": make_blob()})) as get:
            self.assertEqual(licensing.refresh_license()["result"], "no-license")
        get.assert_not_called()

    def test_refresh_off_never_calls_out(self):
        self._apply()
        with override_settings(VIGIL_LICENSE_REFRESH=False), \
                self._answer(FakeResponse(200, {})) as get:
            self.assertEqual(licensing.refresh_license()["result"], "off")
        get.assert_not_called()

    def test_the_request_is_signed_with_the_install_key(self):
        self._apply()
        with self._answer(FakeResponse(200, {"license": make_blob()})) as get:
            licensing.refresh_license()
        params = get.call_args.kwargs["params"]
        key = base64.urlsafe_b64decode(params["key"] + "=" * (-len(params["key"]) % 4))
        sig = base64.urlsafe_b64decode(params["sig"] + "=" * (-len(params["sig"]) % 4))
        self.assertEqual(key, bytes(InstallKey.get().signing_key().verify_key))
        VerifyKey(key).verify(licensing.refresh_message(
            params["instance"], params["ts"], params["version"], params["seats"]), sig)
        self.assertEqual(params["instance"], licensing.instance_id())
        self.assertNotIn("org", params)

    def test_a_newer_license_is_applied(self):
        self._apply(exp_delta=86400 * 10)
        newer = make_blob(exp_delta=86400 * 35)
        with self._answer(FakeResponse(200, {"license": newer})):
            self.assertEqual(licensing.refresh_license()["result"], "renewed")
        self.assertEqual(StoredLicense.current_blob(), newer)

    def test_the_same_license_back_means_this_install_holds_it(self):
        blob = make_blob()
        licensing.set_license(blob)
        with self._answer(FakeResponse(200, {"license": blob})):
            self.assertEqual(licensing.refresh_license()["result"], "current")
        self.assertEqual(StoredLicense.current_blob(), blob)

    def test_nothing_that_fails_to_verify_replaces_the_license(self):
        blob = make_blob(exp_delta=86400 * 10)
        licensing.set_license(blob)
        answers = {
            "forged": make_blob(exp_delta=86400 * 35, sk=OTHER_SK),
            "other instance": make_blob(exp_delta=86400 * 35, instance="00000000-0000-0000-0000-000000000000"),
            "expired": make_blob(exp_delta=-86400 * 60),
        }
        for name, answer in answers.items():
            with self.subTest(name), self._answer(FakeResponse(200, {"license": answer})):
                self.assertEqual(licensing.refresh_license(force=True)["result"], "rejected")
                self.assertEqual(StoredLicense.current_blob(), blob)

    def test_another_install_holding_it_is_reported_and_changes_nothing(self):
        blob = make_blob()
        licensing.set_license(blob)
        with self._answer(FakeResponse(409, {"detail": "x"})):
            outcome = licensing.refresh_license()
        self.assertEqual(outcome["result"], "refused")
        self.assertIn("susquehannasyntax.com/license", outcome["detail"])
        self.assertEqual(StoredLicense.current_blob(), blob)

    def test_an_outage_changes_nothing(self):
        blob = make_blob()
        licensing.set_license(blob)
        for answer in (requests.ConnectionError("down"), FakeResponse(500), FakeResponse(200)):
            with self.subTest(answer), self._answer(answer):
                self.assertEqual(licensing.refresh_license(force=True)["result"], "unreachable")
                self.assertEqual(StoredLicense.current_blob(), blob)

    def test_a_license_from_the_environment_is_not_refreshed(self):
        with mock.patch.dict("os.environ", {"VIGIL_LICENSE_KEY": make_blob()}):
            licensing.reload()
            with self._answer(FakeResponse(200, {})) as get:
                self.assertEqual(licensing.refresh_license()["result"], "env")
            get.assert_not_called()


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB, VIGIL_LICENSE_REFRESH=True)
class RefreshDueTests(TestCase):
    """When the scheduled run calls out: rarely, but at once for a new license."""

    def setUp(self):
        licensing.reload()
        self.row = InstallKey.get()

    def tearDown(self):
        StoredLicense.replace("")
        licensing.reload()

    def _state(self, exp_delta, lid="L1"):
        claims = licensing.Claims(instance=licensing.instance_id(), org="o", seats=1,
                                  exp=int(time.time()) + exp_delta, iat=0, lid=lid)
        return licensing.LicenseState(licensing.Status.VALID, claims=claims, source="db")

    def _settled(self, lid="L1"):
        self.row.refresh_result, self.row.refreshed_lid = "current", lid

    def test_a_new_license_is_claimed_at_once(self):
        self.assertTrue(licensing.refresh_due(self._state(86400 * 34), self.row))

    def test_a_held_license_far_from_expiry_waits(self):
        self._settled()
        self.assertFalse(licensing.refresh_due(self._state(86400 * 30), self.row))

    def test_a_held_license_near_expiry_renews(self):
        self._settled()
        self.assertTrue(licensing.refresh_due(self._state(86400 * 20), self.row))

    def test_a_refused_or_failed_refresh_is_retried(self):
        for result in ("refused", "unreachable", "rejected"):
            with self.subTest(result):
                self.row.refresh_result, self.row.refreshed_lid = result, "L1"
                self.assertTrue(licensing.refresh_due(self._state(86400 * 30), self.row))

    def test_a_different_license_is_claimed_even_when_the_last_one_was_held(self):
        self._settled(lid="OLD")
        self.assertTrue(licensing.refresh_due(self._state(86400 * 30, lid="NEW"), self.row))


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB, VIGIL_LICENSE_REFRESH=True)
class RefreshApiTests(TestCase):
    def setUp(self):
        licensing.reload()
        User = get_user_model()
        self.admin = User.objects.create_user("admin", password="pw")
        UserProfile.objects.update_or_create(user=self.admin, defaults={"role": Role.ADMIN})
        self.viewer = User.objects.create_user("viewer", password="pw")
        UserProfile.objects.update_or_create(user=self.viewer, defaults={"role": Role.VIEWER})
        self.client = APIClient()

    def tearDown(self):
        StoredLicense.replace("")
        licensing.reload()

    def test_pasting_a_license_claims_it_straight_away(self):
        self.client.force_authenticate(self.admin)
        with mock.patch("apps.licensing.views._refresh_in_background") as kick:
            resp = self.client.post("/api/v1/license/", {"license": make_blob()}, format="json")
        self.assertEqual(resp.status_code, 200)
        kick.assert_called_once()

    def test_an_invalid_paste_claims_nothing(self):
        self.client.force_authenticate(self.admin)
        with mock.patch("apps.licensing.views._refresh_in_background") as kick:
            self.client.post("/api/v1/license/", {"license": "junk"}, format="json")
        kick.assert_not_called()

    def test_refresh_now_is_for_admins(self):
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.post("/api/v1/license/refresh/").status_code, 403)

    def test_refresh_now_reports_the_outcome(self):
        licensing.set_license(make_blob())
        self.client.force_authenticate(self.admin)
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
            resp = self.client.post("/api/v1/license/refresh/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["refresh_now"]["result"], "unreachable")
        self.assertEqual(resp.json()["refresh"]["result"], "unreachable")

    def test_the_about_page_names_the_licensee(self):
        licensing.set_license(make_blob())
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get("/api/v1/about/").json()["licensed_to"], "test-org")

