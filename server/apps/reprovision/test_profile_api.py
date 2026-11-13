"""Creating and editing an install profile through the API the editor uses.

The endpoints existed from the start; nothing in the UI reached them, so the
page could only list profiles someone had created by other means. These
cover the paths the editor drives, including the two places where an
obvious-looking form would produce a profile the serializer rejects.
"""
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

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

    def test_responses_match_the_api_schema(self):
        from vigil.schema_check import assert_shape

        from .api_schema import ReprovisionProfileSerializer

        created = self._post(completion_tags=["role:web"])
        self.assertEqual(created.status_code, 201, created.content)
        assert_shape(self, created.json(), ReprovisionProfileSerializer)

        listed = self.client.get("/api/v1/reprovision/profiles/")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertTrue(listed.json(), "the list needs one item to check")
        assert_shape(self, listed.json(), ReprovisionProfileSerializer(many=True))

        detail = self.client.get(
            f"/api/v1/reprovision/profiles/{created.json()['id']}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        assert_shape(self, detail.json(), ReprovisionProfileSerializer)

        patched = self.client.patch(
            f"/api/v1/reprovision/profiles/{created.json()['id']}/",
            json.dumps({"disk_target": "/dev/nvme0n1"}),
            content_type="application/json")
        self.assertEqual(patched.status_code, 200, patched.content)
        assert_shape(self, patched.json(), ReprovisionProfileSerializer)

    def test_a_preview_matches_the_api_schema(self):
        """``files`` is keyed by the family's renderer; Ubuntu's two files are
        what puts a second key through the check."""
        from vigil.schema_check import assert_shape

        from .api_schema import ReprovisionPreviewSerializer

        profile_id = self._post().json()["id"]
        host = Host.objects.create(hostname="preview-01", agent_token="t" * 32)
        resp = self.client.post(
            f"/api/v1/reprovision/profiles/{profile_id}/preview/",
            json.dumps({"host": str(host.id)}), content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertGreater(len(body["files"]), 1,
                           "the check wants both of Ubuntu's rendered files")
        assert_shape(self, body, ReprovisionPreviewSerializer)

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

    def test_the_ceremony_and_a_job_answer_refuse_too(self):
        """The list, the ceremony's 201, the detail route and an abort all
        render ``RebuildJobSerializer``, so they are checked as one."""
        from unittest.mock import patch

        from vigil.schema_check import assert_shape

        from .api_schema import ReprovisionJobSerializer

        profile_id = self._post().json()["id"]
        host = Host.objects.create(hostname="web-01", agent_token="t" * 32)
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            created = self.client.post("/api/v1/reprovision/jobs/", {
                "host": str(host.id), "image": str(self.image.id),
                "profile": profile_id, "password": "x",
                "typed_hostname": "web-01",
                "acknowledge_plaintext_transport": True,
            }, content_type="application/json")
        self.assertEqual(created.status_code, 201, created.content)
        job_id = created.json()["id"]
        assert_shape(self, created.json(), ReprovisionJobSerializer)

        listed = self.client.get("/api/v1/reprovision/jobs/")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertTrue(listed.json(), "the list needs one item to check")
        assert_shape(self, listed.json(), ReprovisionJobSerializer(many=True))

        detail = self.client.get(f"/api/v1/reprovision/jobs/{job_id}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        assert_shape(self, detail.json(), ReprovisionJobSerializer)

        aborted = self.client.post(f"/api/v1/reprovision/jobs/{job_id}/abort/",
                                   {}, content_type="application/json")
        self.assertEqual(aborted.status_code, 200, aborted.content)
        assert_shape(self, aborted.json(), ReprovisionJobSerializer)


class RebuildRefusalShapeTests(TestCase):
    """The refusals a Viewer draws on the two job routes match the error
    shape in the schema: the view's own ``error`` sentence, and DRF's
    ``detail`` from ``get_object_or_404``."""

    def setUp(self):
        self.host = Host.objects.create(hostname="refuse-01",
                                        agent_token=f"tok-{uuid.uuid4()}")
        user = get_user_model().objects.create_user("pleb", password="x")
        UserProfile.objects.create(user=user, role=Role.VIEWER)
        self.client.force_login(user)

    def test_a_rebuild_a_viewer_may_not_run_is_refused(self):
        """A Viewer sees the host but may not rebuild it: ``_may(user, host,
        "rebuild")`` failing answers 403 with ``error``."""
        from vigil.schema_check import assert_shape

        from .api_schema import ReprovisionErrorSerializer

        resp = self.client.post(
            "/api/v1/reprovision/jobs/", {"host": str(self.host.id)},
            content_type="application/json")
        self.assertEqual(resp.status_code, 403, resp.content)
        assert_shape(self, resp.json(), ReprovisionErrorSerializer)

    def test_a_job_that_is_not_there_cannot_be_aborted(self):
        """``get_object_or_404``, which answers 404 with DRF's ``detail``."""
        from vigil.schema_check import assert_shape

        from .api_schema import ReprovisionErrorSerializer

        resp = self.client.post(
            f"/api/v1/reprovision/jobs/{uuid.uuid4()}/abort/", {},
            content_type="application/json")
        self.assertEqual(resp.status_code, 404, resp.content)
        assert_shape(self, resp.json(), ReprovisionErrorSerializer)
