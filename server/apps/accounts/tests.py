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
