from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import LoginAttempt, UserProfile
from apps.accounts.totp import (
    consume_totp,
    generate_secret,
    generate_totp,
    verify_totp,
)
from apps.accounts.views import MAX_FAILURES_PER_USERNAME


class TotpSecretEncryptionTests(TestCase):
    """The TOTP secret must never be stored in plaintext."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("alice", password="pw")

    def test_secret_is_encrypted_at_rest(self):
        secret = generate_secret()
        profile = UserProfile.objects.create(user=self.user)
        profile.totp_secret = secret
        profile.save()

        raw = UserProfile.objects.values_list(
            "totp_secret_encrypted", flat=True
        ).get(pk=profile.pk)
        self.assertTrue(bytes(raw), "expected ciphertext to be stored")
        self.assertNotIn(secret.encode(), bytes(raw))

        reloaded = UserProfile.objects.get(pk=profile.pk)
        self.assertEqual(reloaded.totp_secret, secret)

    def test_empty_secret_round_trips(self):
        profile = UserProfile.objects.create(user=self.user)
        self.assertEqual(profile.totp_secret, "")
        profile.totp_secret = "ABCDEFGHIJKLMNOP"
        profile.save()
        profile.totp_secret = ""
        profile.save()
        self.assertEqual(UserProfile.objects.get(pk=profile.pk).totp_secret, "")


class TotpAlgorithmTests(TestCase):
    def test_generate_and_verify(self):
        secret = generate_secret()
        code = generate_totp(secret)
        self.assertTrue(verify_totp(secret, code))
        wrong = "000000" if code != "000000" else "111111"
        self.assertFalse(verify_totp(secret, wrong))

    def test_replay_is_rejected(self):
        user = get_user_model().objects.create_user("bob", password="pw")
        profile = UserProfile.objects.create(user=user)
        profile.totp_secret = generate_secret()
        profile.totp_confirmed_at = now()
        profile.save()
        code = generate_totp(profile.totp_secret)

        ok, err = consume_totp(user, code)
        self.assertTrue(ok, err)
        ok_again, _ = consume_totp(user, code)
        self.assertFalse(ok_again, "a TOTP code must not be accepted twice")


class SetupFlowTests(TestCase):
    """Regression coverage — setup must not authenticate the browser until TOTP
    has been confirmed."""

    def test_step1_creates_account_without_logging_in(self):
        resp = self.client.post(
            "/setup/",
            {"username": "admin", "password": "supersecret1", "confirm": "supersecret1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(get_user_model().objects.filter(username="admin").exists())
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn("setup_totp_secret", self.client.session)

    def test_step2_logs_in_after_totp_confirmed(self):
        self.client.post(
            "/setup/",
            {"username": "admin", "password": "supersecret1", "confirm": "supersecret1"},
        )
        secret = self.client.session["setup_totp_secret"]
        resp = self.client.post("/setup/", {"totp_code": generate_totp(secret)})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

        profile = UserProfile.objects.get(user__username="admin")
        self.assertIsNotNone(profile.totp_confirmed_at)
        self.assertEqual(profile.totp_secret, secret)


class LoginRateLimitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("alice", password="correct-horse1")

    def _fail(self, username="alice"):
        return self.client.post("/login/", {"username": username, "password": "wrong"})

    def test_lockout_after_repeated_failures_for_username(self):
        for _ in range(MAX_FAILURES_PER_USERNAME):
            resp = self._fail()
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            "/login/", {"username": "alice", "password": "correct-horse1"}
        )
        self.assertEqual(resp.status_code, 429)

    def test_successful_login_clears_failures(self):
        for _ in range(MAX_FAILURES_PER_USERNAME - 1):
            self._fail()
        resp = self.client.post(
            "/login/", {"username": "alice", "password": "correct-horse1"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(LoginAttempt.objects.filter(username="alice").exists())

    def test_old_failures_fall_out_of_window(self):
        for _ in range(MAX_FAILURES_PER_USERNAME):
            self._fail()
        LoginAttempt.objects.update(created_at=now() - timedelta(hours=1))
        resp = self.client.post(
            "/login/", {"username": "alice", "password": "correct-horse1"}
        )
        self.assertEqual(resp.status_code, 302)

    def test_login_rejects_offsite_next_redirect(self):
        resp = self.client.post(
            "/login/?next=https://evil.example.com/",
            {"username": "alice", "password": "correct-horse1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")


class AdminGateTests(TestCase):
    """Privileged endpoints must check the admin role server-side."""

    def setUp(self):
        self.client = APIClient()
        self.viewer = get_user_model().objects.create_user("viewer", password="pw")

    def test_upload_agent_requires_admin(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.post("/agent/upload/linux-amd64/")
        self.assertEqual(resp.status_code, 403)

    def test_ad_config_requires_admin(self):
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get("/api/v1/hosts/ad/").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/hosts/ad/", {}).status_code, 403)

    def test_admin_user_passes_gate(self):
        admin = get_user_model().objects.create_user(
            "admin2", password="pw", is_staff=True
        )
        self.client.force_authenticate(admin)
        self.assertEqual(self.client.get("/api/v1/hosts/ad/").status_code, 200)

    def test_host_approve_requires_admin(self):
        from apps.hosts.models import Host

        host = Host.objects.create(
            hostname="pending-h", agent_token="t" * 32, status=Host.Status.PENDING,
        )
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(f"/api/v1/hosts/{host.id}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_host_delete_requires_admin(self):
        from apps.hosts.models import Host

        host = Host.objects.create(
            hostname="h", agent_token="u" * 32, status=Host.Status.ONLINE,
        )
        self.client.force_authenticate(self.viewer)
        resp = self.client.delete(f"/api/v1/hosts/{host.id}/")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Host.objects.filter(pk=host.id).exists())
        # Viewing the same host stays open to any authenticated session
        self.assertEqual(self.client.get(f"/api/v1/hosts/{host.id}/").status_code, 200)


# ---------------------------------------------------------------------------
# RBAC + seats
# ---------------------------------------------------------------------------

import base64 as _b64
import json as _json
import time as _time

from django.test import override_settings
from nacl.signing import SigningKey as _SigningKey

from vigil import licensing as _licensing

from .models import Role, UserProfile as _UP
from .permissions import IsOperator, role_of

_SK = _SigningKey.generate()
_PUB = _b64.b64encode(_SK.verify_key.encode()).decode()


def _blob():
    claims = {
        "instance": _licensing.instance_id(), "org": "t", "seats": 2,
        "sites": None, "exp": int(_time.time()) + 86400, "iat": int(_time.time()),
    }
    payload = _json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return _b64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{_licensing.PREFIX}.{b64u(payload)}.{b64u(_SK.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=_PUB)
class RbacSeatTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(self.admin)
        _licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        _licensing.reload()

    def test_role_of_mapping(self):
        User = get_user_model()
        self.assertEqual(role_of(self.admin), Role.ADMIN)  # staff → admin
        plain = User.objects.create_user("plain", password="x")
        self.assertEqual(role_of(plain), Role.VIEWER)      # no profile → viewer
        _UP.objects.create(user=plain, role=Role.OPERATOR)
        fresh = User.objects.get(pk=plain.pk)  # uncached profile relation
        self.assertEqual(role_of(fresh), Role.OPERATOR)

    def test_viewer_and_admin_roles_are_free(self):
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "v", "password": "pw12345!",
                                 "role": "viewer"})
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = self.client.patch(
            f"/api/v1/accounts/users/{resp.json()['id']}/role/",
            {"role": "admin"}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["role"], "admin")

    def test_operator_role_requires_business(self):
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "op1", "password": "pw12345!",
                                 "role": "operator"})
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["feature"], "rbac_advanced")
        # the half-created user must not linger
        self.assertFalse(get_user_model().objects.filter(username="op1").exists())
        _licensing.set_license(_blob())
        resp = self.client.post("/api/v1/accounts/users/",
                                {"username": "op1", "password": "pw12345!",
                                 "role": "operator"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["role"], "operator")

    def test_operator_keeps_working_after_lapse(self):
        _licensing.set_license(_blob())
        self.client.post("/api/v1/accounts/users/",
                         {"username": "op2", "password": "pw12345!",
                          "role": "operator"})
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        _licensing.reload()  # license gone
        op = get_user_model().objects.get(username="op2")
        self.assertTrue(IsOperator().has_permission(
            type("R", (), {"user": op})(), None))

    def test_seat_overage_never_blocks_creation(self):
        _licensing.set_license(_blob())  # 2 seats
        for i in range(4):               # ends at 5 users total
            resp = self.client.post("/api/v1/accounts/users/",
                                    {"username": f"u{i}", "password": "pw12345!"})
            self.assertEqual(resp.status_code, 201)
        self.assertEqual(_licensing.seats_used(), 5)
        overage = [b for b in _licensing.banners() if "seats in use" in b["message"]]
        self.assertEqual(len(overage), 1)

    def test_non_admin_cannot_manage_users(self):
        get_user_model().objects.create_user("pleb", password="x")
        c = self.client_class()
        c.login(username="pleb", password="x")
        self.assertEqual(c.get("/api/v1/accounts/users/").status_code, 403)
