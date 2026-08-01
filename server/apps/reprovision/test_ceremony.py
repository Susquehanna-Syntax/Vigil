"""Three factors, all required (§4.4)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.reprovision.ceremony import verify_rebuild_confirmation

User = get_user_model()


class CeremonyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ops", password="correct-horse-battery")
        self.host = Host.objects.create(
            hostname="web-01", agent_token="t", status=Host.Status.ONLINE)

    def _payload(self, **over):
        base = {"password": "correct-horse-battery", "totp": "123456",
                "typed_hostname": "web-01"}
        base.update(over)
        return base

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_all_three_factors_passes(self, _totp):
        self.assertIsNone(
            verify_rebuild_confirmation(self.user, self._payload(), self.host))

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_wrong_password_fails(self, _totp):
        err = verify_rebuild_confirmation(
            self.user, self._payload(password="wrong"), self.host)
        self.assertIn("password", err.lower())

    @patch("apps.accounts.totp.require_totp_confirmation",
           return_value="Invalid code")
    def test_bad_totp_fails_even_with_a_correct_password(self, _totp):
        self.assertIsNotNone(
            verify_rebuild_confirmation(self.user, self._payload(), self.host))

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_wrong_hostname_fails(self, _totp):
        err = verify_rebuild_confirmation(
            self.user, self._payload(typed_hostname="web-02"), self.host)
        self.assertIn("hostname", err.lower())

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_hostname_comparison_is_exact_not_fuzzy(self, _totp):
        # "close enough" is exactly what this factor exists to prevent.
        for typed in ("WEB-01", " web-01", "web-0", "web-01 "):
            self.assertIsNotNone(
                verify_rebuild_confirmation(
                    self.user, self._payload(typed_hostname=typed), self.host),
                typed)

    def test_missing_factors_fail(self):
        # The TOTP helper is stubbed with its real contract rather than a
        # blanket success: a stub that accepts an empty code would make this
        # test pass while the missing-TOTP case went unchecked.
        def fake_totp(_user, payload):
            return None if (payload or {}).get("totp") else "TOTP code required"

        with patch("apps.accounts.totp.require_totp_confirmation",
                   side_effect=fake_totp):
            for missing in ("password", "totp", "typed_hostname"):
                payload = self._payload()
                payload[missing] = ""
                self.assertIsNotNone(
                    verify_rebuild_confirmation(self.user, payload, self.host),
                    missing)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_empty_payload_fails(self, _totp):
        self.assertIsNotNone(
            verify_rebuild_confirmation(self.user, None, self.host))

    def test_real_totp_helper_rejects_an_empty_code(self):
        """Guards the assumption the stub above encodes."""
        from apps.accounts.totp import require_totp_confirmation

        self.assertIsNotNone(require_totp_confirmation(self.user, {"totp": ""}))
