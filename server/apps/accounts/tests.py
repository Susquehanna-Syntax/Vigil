from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.accounts.models import UserProfile
from apps.accounts.totp import (
    consume_totp,
    generate_secret,
    generate_totp,
    verify_totp,
)


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
