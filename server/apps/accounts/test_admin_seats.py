"""Free tier stops at two Admins; Business bills per technician seat.

Seats are counted (never blocked) for operators — an overage is a banner, §6.
The Admin cap is different: a hard 402 at the moment a role is *granted*,
and nothing else. Existing Admins past the cap keep working.
"""
import base64 as _b64
import json as _json
import time as _time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey as _SigningKey
from rest_framework.test import APIClient

from apps.accounts.models import Role
from vigil import licensing

_SK = _SigningKey.generate()
_PUB = _b64.b64encode(_SK.verify_key.encode()).decode()


def _blob(seats=2):
    claims = {
        "instance": licensing.instance_id(), "org": "t", "seats": seats,
        "sites": None, "exp": int(_time.time()) + 86400, "iat": int(_time.time()),
    }
    payload = _json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return _b64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(_SK.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=_PUB)
class AdminCapTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = get_user_model().objects.create_user(
            "root", password="pw12345!", is_staff=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    # ── Seat counting: technicians only ────────────────────────────────

    def test_viewers_do_not_consume_seats(self):
        for i in range(3):
            self.client.post("/api/v1/accounts/users/",
                             {"username": f"v{i}", "password": "pw12345!"})
        self.assertEqual(licensing.seats_used(), 1)  # just the logged-in admin

    def test_an_operator_consumes_a_seat(self):
        licensing.set_license(_blob())  # business — OPERATOR is grantable
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "op1", "password": "pw12345!",
                                 "role": "operator"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(licensing.seats_used(), 2)  # admin + operator

    def test_an_admin_consumes_a_seat(self):
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "a2", "password": "pw12345!",
                                 "role": "admin"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(licensing.seats_used(), 2)

    def test_an_inactive_admin_does_not_consume_a_seat(self):
        admin2 = get_user_model().objects.create_user(
            "a2", password="pw12345!", is_staff=True)
        admin2.is_active = False
        admin2.save(update_fields=["is_active"])
        self.assertEqual(licensing.seats_used(), 1)

    # ── The Free Admin cap, through the real API ───────────────────────

    def test_free_refuses_a_third_admin_at_creation(self):
        self.client.post("/api/v1/accounts/users/",
                         {"username": "a2", "password": "pw12345!",
                          "role": "admin"})
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "a3", "password": "pw12345!",
                                 "role": "admin"})
        self.assertEqual(resp.status_code, 402, resp.content)
        self.assertFalse(get_user_model().objects.filter(username="a3").exists())

    def test_free_refuses_a_third_admin_at_promotion(self):
        self.client.post("/api/v1/accounts/users/",
                         {"username": "a2", "password": "pw12345!",
                          "role": "admin"})
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "v1", "password": "pw12345!",
                                 "role": "viewer"})
        self.assertEqual(resp.status_code, 201, resp.content)
        viewer = get_user_model().objects.get(username="v1")
        resp = self.client.patch(
            f"/api/v1/accounts/users/{viewer.pk}/role/",
            {"role": "admin"}, content_type="application/json")
        self.assertEqual(resp.status_code, 402, resp.content)
        viewer.refresh_from_db()
        self.assertEqual(viewer.profile.role, Role.VIEWER)

    def test_the_refusal_is_the_402_upgrade_body(self):
        self.client.post("/api/v1/accounts/users/",
                         {"username": "a2", "password": "pw12345!",
                          "role": "admin"})
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "a3", "password": "pw12345!",
                                 "role": "admin"})
        self.assertEqual(resp.status_code, 402, resp.content)
        body = resp.json()
        self.assertEqual(body["feature"], "admin_seats")
        self.assertFalse(body["licensed"])
        self.assertIn("detail", body)
        self.assertIn("upgrade_url", body)

    def test_free_still_allows_unlimited_viewers(self):
        self.client.post("/api/v1/accounts/users/",
                         {"username": "a2", "password": "pw12345!",
                          "role": "admin"})
        for i in range(5):
            resp = self.client.post("/api/v1/accounts/users/",
                                    {"username": f"v{i}", "password": "pw12345!",
                                     "role": "viewer"})
            self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(licensing.seats_used(), 2)  # viewers never count

    def test_re_applying_admin_to_an_existing_admin_is_allowed(self):
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "a2", "password": "pw12345!",
                                 "role": "admin"})
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = self.client.patch(
            f"/api/v1/accounts/users/{resp.json()['id']}/role/",
            {"role": "admin"}, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_business_has_no_admin_cap(self):
        licensing.set_license(_blob())
        self.client.post("/api/v1/accounts/users/",
                         {"username": "a2", "password": "pw12345!",
                          "role": "admin"})
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "a3", "password": "pw12345!",
                                 "role": "admin"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(licensing.seats_used(), 3)

    def test_existing_admins_past_the_cap_keep_working(self):
        User = get_user_model()
        # root + a0 + a1: three admins on an unlicensed box, built straight
        # in the ORM — none of them created through the gated API.
        for i in range(2):
            User.objects.create_user(f"a{i}", password="pw12345!", is_staff=True)
        admin2 = User.objects.get(username="a0")
        self.client.force_authenticate(admin2)
        resp = self.client.get("/api/v1/accounts/users/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["users"]), 3)
