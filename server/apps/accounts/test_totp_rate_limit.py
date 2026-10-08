"""TOTP-gated actions stop accepting codes after repeated wrong ones (architect
review, 2026-10-08). The gate exists for a session that someone holds without
the device; without a limit, a six-digit code falls to guessing."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.accounts.models import UserProfile
from apps.accounts.totp import MAX_TOTP_FAILURES, consume_totp, generate_secret, generate_totp


class TotpRateLimitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="pw")
        profile = UserProfile.objects.create(user=self.user)
        self.secret = generate_secret()
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()

    def _wrong(self):
        good = generate_totp(self.secret)
        return "000000" if good != "000000" else "111111"

    def test_after_the_limit_even_the_right_code_is_refused(self):
        for _ in range(MAX_TOTP_FAILURES):
            self.assertFalse(consume_totp(self.user, self._wrong())[0])
        ok, err = consume_totp(self.user, generate_totp(self.secret))
        self.assertFalse(ok)
        self.assertIn("Too many wrong codes", err)

    def test_a_right_code_before_the_limit_clears_the_count(self):
        for _ in range(MAX_TOTP_FAILURES - 1):
            consume_totp(self.user, self._wrong())
        self.assertTrue(consume_totp(self.user, generate_totp(self.secret))[0])
        for _ in range(MAX_TOTP_FAILURES - 1):
            consume_totp(self.user, self._wrong())
        self.assertNotIn("Too many", consume_totp(self.user, self._wrong())[1])

    def test_failed_logins_cannot_lock_someone_out(self):
        # Failed sign-ins with a crafted username used to share the count.
        from apps.accounts.models import LoginAttempt
        for _ in range(20):
            LoginAttempt.objects.create(username=f"totp:{self.user.pk}", ip="203.0.113.9")
        self.assertTrue(consume_totp(self.user, generate_totp(self.secret))[0])

    def test_an_attempt_is_recorded_before_the_code_is_checked(self):
        # Record-then-count is what stops parallel guesses all passing a check
        # none of them had incremented yet.
        from unittest.mock import patch

        from apps.accounts import totp
        from apps.accounts.models import TotpAttempt
        seen = []
        real = totp.verify_totp
        with patch.object(totp, "verify_totp",
                          side_effect=lambda s, c: seen.append(TotpAttempt.objects.filter(user=self.user).count())
                          or real(s, c)):
            consume_totp(self.user, self._wrong())
        self.assertEqual(seen, [1])
