"""Creating and editing an install profile through the API the editor uses.

The endpoints existed from the start; nothing in the UI reached them, so the
page could only list profiles someone had created by other means. These
cover the paths the editor drives, including the two places where an
obvious-looking form would produce a profile the serializer rejects.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import InstallProfile, OSImage


def make_image(status=OSImage.Status.READY):
    return OSImage.objects.create(
        name="Ubuntu Server 24.04 LTS", os_family=OSImage.Family.UBUNTU,
        version="24.04", architecture="x86_64", sha256="a" * 64, status=status)


class ProfileApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.image = make_image()

    def _body(self, **extra):
        return {
            "name": "Web server", "image": str(self.image.id),
            "disk_target": "/dev/sda", "partition_scheme": "lvm",
            "filesystem": "ext4", "network_mode": "dhcp",
            "static_address": "", "gateway": "", "dns": "",
            "timezone": "UTC", "locale": "en_US.UTF-8", "keyboard": "us",
            "admin_username": "admin",
            "ssh_authorized_keys": "ssh-ed25519 AAAAC3Nz test@laptop",
            "raw_append": "", "extra_packages": ["curl", "vim"],
            "deadline_minutes": 60, **extra}

    def _post(self, **extra):
        return self.client.post("/api/v1/reprovision/profiles/",
                                json.dumps(self._body(**extra)),
                                content_type="application/json")

    def test_a_profile_is_created_from_the_editor_payload(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        profile = InstallProfile.objects.get()
        self.assertEqual(profile.name, "Web server")
        self.assertEqual(profile.extra_packages, ["curl", "vim"])

    def test_the_stored_password_is_never_returned(self):
        """Not even to the admin who set it."""
        resp = self._post(admin_password="$6$rounds=5000$abc")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertNotIn("admin_password", resp.json())
        self.assertNotIn("admin_password_encrypted", resp.json())

    def test_a_profile_nobody_can_log_into_is_refused(self):
        """Neither a key nor a password hash — caught here rather than after
        the disk is wiped."""
        resp = self._post(ssh_authorized_keys="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("log in", json.dumps(resp.json()).lower())

    def test_static_networking_without_an_address_is_refused(self):
        resp = self._post(network_mode="static")
        self.assertEqual(resp.status_code, 400)

    def test_static_networking_with_an_address_and_gateway_is_accepted(self):
        resp = self._post(network_mode="static",
                          static_address="192.168.1.50/24",
                          gateway="192.168.1.1")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_editing_a_profile_without_resending_the_password_is_allowed(self):
        """The editor cannot resend it — the API never returns it. A blank
        box has to mean "keep what is stored", or every edit of a
        password-only profile would fail the can-anyone-log-in check.
        """
        created = self._post(ssh_authorized_keys="",
                             admin_password="$6$rounds=5000$abc")
        self.assertEqual(created.status_code, 201, created.content)
        profile_id = created.json()["id"]
        stored = InstallProfile.objects.get(pk=profile_id).admin_password_encrypted

        resp = self.client.patch(
            f"/api/v1/reprovision/profiles/{profile_id}/",
            json.dumps({"disk_target": "/dev/nvme0n1"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        profile = InstallProfile.objects.get(pk=profile_id)
        self.assertEqual(profile.disk_target, "/dev/nvme0n1")
        self.assertEqual(profile.admin_password_encrypted, stored,
                         "an unrelated edit must not disturb the password")

    def test_editing_can_replace_the_password(self):
        created = self._post(admin_password="$6$old")
        profile_id = created.json()["id"]
        before = InstallProfile.objects.get(pk=profile_id).admin_password_encrypted
        resp = self.client.patch(
            f"/api/v1/reprovision/profiles/{profile_id}/",
            json.dumps({"admin_password": "$6$new"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        after = InstallProfile.objects.get(pk=profile_id).admin_password_encrypted
        self.assertNotEqual(before, after)

    def test_a_profile_can_be_deleted(self):
        profile_id = self._post().json()["id"]
        resp = self.client.delete(f"/api/v1/reprovision/profiles/{profile_id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(InstallProfile.objects.exists())

    def test_a_non_admin_cannot_create_a_profile(self):
        self.client.force_login(
            get_user_model().objects.create_user("viewer", password="x"))
        self.assertEqual(self._post().status_code, 403)
