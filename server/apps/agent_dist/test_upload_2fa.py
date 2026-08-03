"""Replacing the agent binary must be 2FA-gated.

Its own docstring says it: "whoever can replace the served binary controls
code that runs as root on every enrolled host." That is a strictly larger
blast radius than approving a single host or deploying one task — both of
which already require a TOTP code. A hijacked or borrowed admin session
should not be enough to push a backdoored binary to the whole fleet.
"""
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile

User = get_user_model()


class UploadAgentConfirmationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="a", password="pw")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)

    def _upload(self, **extra):
        payload = {"binary": BytesIO(b"fake binary"), "version": "1.0"}
        payload.update(extra)
        return self.client.post(
            "/agent/upload/linux-amd64/", payload)

    def test_admin_without_totp_is_refused(self):
        self.client.force_login(self.admin)
        resp = self._upload()
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_admin_with_valid_totp_succeeds(self):
        self.client.force_login(self.admin)
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self._upload(totp="123456")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_bad_totp_is_refused(self):
        self.client.force_login(self.admin)
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value="Invalid code"):
            resp = self._upload(totp="000000")
        self.assertEqual(resp.status_code, 401)

    def test_nothing_is_stored_when_confirmation_fails(self):
        from apps.agent_dist.models import AgentBinary

        self.client.force_login(self.admin)
        self._upload()
        self.assertFalse(AgentBinary.objects.exists())

    def test_non_admin_still_refused_before_totp(self):
        viewer = User.objects.create_user(username="v", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        self.client.force_login(viewer)
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self._upload(totp="123456")
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_is_refused(self):
        self.assertIn(self._upload().status_code, (401, 403))
