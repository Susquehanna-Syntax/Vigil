"""The standing acknowledgement that this instance is served over plain HTTP.

Vigil hands out secrets that only TLS protects, so the rebuild ceremony
refuses on plain HTTP without an acknowledgement. Making it once, in Settings,
by a named admin is the point — it is a decision about the instance, not a box
inside every rebuild.
"""
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host, TransportAck
from apps.reprovision.models import InstallProfile, OSImage

User = get_user_model()

URL = "/api/v1/hosts/transport-ack/"


class TransportAckEndpointTests(TestCase):
    def _login(self, role=Role.ADMIN):
        user = User.objects.create_user(username=f"u-{role}-{uuid.uuid4().hex[:6]}",
                                        password="pw")
        UserProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        return user

    def test_it_starts_unacknowledged(self):
        self._login()
        body = self.client.get(URL).json()
        self.assertFalse(body["acknowledged"])
        self.assertEqual(body["acknowledged_by"], "")

    def test_acknowledging_records_who_and_when(self):
        user = self._login()
        body = self.client.post(URL, {"acknowledged": True},
                                content_type="application/json").json()
        self.assertTrue(body["acknowledged"])
        self.assertEqual(body["acknowledged_by"], user.username)
        self.assertIsNotNone(body["acknowledged_at"])

    def test_withdrawing_clears_the_name(self):
        # A name left beside a withdrawn acknowledgement reads as though it
        # still stands.
        self._login()
        self.client.post(URL, {"acknowledged": True},
                         content_type="application/json")
        body = self.client.post(URL, {"acknowledged": False},
                                content_type="application/json").json()
        self.assertFalse(body["acknowledged"])
        self.assertEqual(body["acknowledged_by"], "")
        self.assertIsNone(body["acknowledged_at"])

    def test_it_is_a_singleton(self):
        self._login()
        self.client.post(URL, {"acknowledged": True},
                         content_type="application/json")
        self.client.post(URL, {"acknowledged": False},
                         content_type="application/json")
        self.assertEqual(TransportAck.objects.count(), 1)

    def test_a_viewer_cannot_read_or_set_it(self):
        self._login(Role.VIEWER)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(
            self.client.post(URL, {"acknowledged": True},
                             content_type="application/json").status_code, 403)

    def test_anonymous_cannot_set_it(self):
        self.assertIn(
            self.client.post(URL, {"acknowledged": True},
                             content_type="application/json").status_code,
            (401, 403))


class RebuildHonoursTheStandingAckTests(TestCase):
    """The whole point of recording it: rebuilds stop asking."""

    def setUp(self):
        self.image = OSImage.objects.create(
            name="U", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="f" * 64,
            status=OSImage.Status.READY)
        self.profile = InstallProfile.objects.create(
            name="std", image=self.image, disk_target="/dev/sda",
            admin_username="v")
        self.host = Host.objects.create(
            hostname="web-01", agent_token=f"tok-{uuid.uuid4()}",
            status=Host.Status.ONLINE)
        user = User.objects.create_user(username="admin-x", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client.force_login(user)

    def _create(self):
        # No acknowledge_plaintext_transport in the body — that is the point.
        return self.client.post("/api/v1/reprovision/jobs/", {
            "host": str(self.host.id), "image": str(self.image.id),
            "profile": str(self.profile.id), "password": "pw",
            "totp": "123456", "typed_hostname": "web-01",
        }, content_type="application/json")

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_without_the_ack_a_rebuild_is_refused(self, _totp):
        resp = self._create()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get("code"), "plaintext_transport")

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_with_the_standing_ack_a_rebuild_proceeds(self, _totp):
        ack = TransportAck.load()
        ack.acknowledged = True
        ack.save()
        self.assertEqual(self._create().status_code, 201)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_withdrawing_it_starts_refusing_again(self, _totp):
        ack = TransportAck.load()
        ack.acknowledged = True
        ack.save()
        self.assertEqual(self._create().status_code, 201)
        ack.acknowledged = False
        ack.save()
        self.assertEqual(self._create().status_code, 400)
