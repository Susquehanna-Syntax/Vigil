import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import CAPABILITIES, LEGACY_OPERATOR
from apps.hosts.models import Host
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob

User = get_user_model()


class CapabilityVocabularyTests(TestCase):
    def test_reprovision_app_registered(self):
        self.assertEqual(CAPABILITIES["reprovision"],
                         frozenset({"view", "rebuild"}))

    def test_legacy_operators_get_no_rebuild_rights(self):
        # Turning this feature on must not retroactively hand every existing
        # operator the ability to wipe machines.
        apps = {app for app, _verb in LEGACY_OPERATOR}
        self.assertNotIn("reprovision", apps)

    def test_image_management_is_not_a_delegatable_capability(self):
        # Uploading an ISO chooses what runs as root on every rebuilt machine,
        # so it stays admin-only and out of the operator matrix (§4.4).
        self.assertNotIn("manage_images", CAPABILITIES["reprovision"])


class RebuildEndpointAuthTests(TestCase):
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

    def _login(self, role):
        user = User.objects.create_user(username=f"u-{role}", password="pw")
        UserProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        return user

    def _create(self, **over):
        body = {
            "host": str(self.host.id), "image": str(self.image.id),
            "profile": str(self.profile.id), "password": "pw",
            "totp": "123456", "typed_hostname": "web-01",
            "acknowledge_plaintext_transport": True,
        }
        body.update(over)
        return self.client.post("/api/v1/reprovision/jobs/", body,
                                content_type="application/json")

    def test_anonymous_cannot_create_a_job(self):
        self.assertIn(self._create().status_code, (401, 403))

    def test_viewer_cannot_create_a_job(self):
        self._login(Role.VIEWER)
        self.assertEqual(self._create().status_code, 403)

    def test_viewer_cannot_upload_an_image(self):
        self._login(Role.VIEWER)
        resp = self.client.post("/api/v1/reprovision/images/", {
            "name": "x", "os_family": "ubuntu", "version": "1",
            "architecture": "x86_64", "sha256": "0" * 64,
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_viewer_cannot_abort_a_job(self):
        from datetime import timedelta

        from django.utils.timezone import now

        job = RebuildJob.objects.create(
            host=self.host, image=self.image, profile=self.profile,
            deadline=now() + timedelta(minutes=60))
        self._login(Role.VIEWER)
        resp = self.client.post(
            f"/api/v1/reprovision/jobs/{job.id}/abort/", {},
            content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_admin_can_create_a_job(self, _totp):
        self._login(Role.ADMIN)
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_job_rejects_a_non_ready_image(self, _totp):
        self._login(Role.ADMIN)
        self.image.status = OSImage.Status.IMPORTING
        self.image.save(update_fields=["status"])
        self.assertEqual(self._create().status_code, 400)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_job_rejects_a_reserved_tag_namespace(self, _totp):
        self._login(Role.ADMIN)
        resp = self._create(completion_tag="agent:rebuilt")
        self.assertEqual(resp.status_code, 400)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_job_rejects_a_profile_from_another_image(self, _totp):
        other = OSImage.objects.create(
            name="R", os_family=OSImage.Family.RHEL, version="9",
            architecture="x86_64", sha256="a" * 64,
            status=OSImage.Status.READY)
        stray = InstallProfile.objects.create(
            name="stray", image=other, disk_target="/dev/sda",
            admin_username="v")
        self._login(Role.ADMIN)
        self.assertEqual(
            self._create(profile=str(stray.id)).status_code, 400)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_plaintext_transport_must_be_acknowledged(self, _totp):
        self._login(Role.ADMIN)
        resp = self._create(acknowledge_plaintext_transport=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get("code"), "plaintext_transport")

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_wrong_typed_hostname_is_refused(self, _totp):
        self._login(Role.ADMIN)
        self.assertEqual(
            self._create(typed_hostname="web-02").status_code, 401)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_wrong_password_is_refused(self, _totp):
        self._login(Role.ADMIN)
        self.assertEqual(self._create(password="nope").status_code, 401)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_created_job_has_tokens_and_a_deadline(self, _totp):
        self._login(Role.ADMIN)
        job_id = self._create().json()["id"]
        job = RebuildJob.objects.get(pk=job_id)
        self.assertTrue(job.answer_token_hash)
        self.assertTrue(job.enroll_token_hash)
        self.assertIsNotNone(job.deadline)

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_abort_after_the_point_of_no_return_is_a_conflict(self, _totp):
        from datetime import timedelta

        from django.utils.timezone import now

        job = RebuildJob.objects.create(
            host=self.host, image=self.image, profile=self.profile,
            state=RebuildJob.State.INSTALLING,
            deadline=now() + timedelta(minutes=60))
        self._login(Role.ADMIN)
        resp = self.client.post(
            f"/api/v1/reprovision/jobs/{job.id}/abort/", {},
            content_type="application/json")
        self.assertEqual(resp.status_code, 409)


class ProfileValidationTests(TestCase):
    """A profile with no way to log in produces a machine nobody can reach."""

    def setUp(self):
        self.image = OSImage.objects.create(
            name="U", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="b" * 64,
            status=OSImage.Status.READY)
        user = User.objects.create_user(username="admin1", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client.force_login(user)

    def _post(self, **over):
        body = {"name": "p", "image": str(self.image.id),
                "disk_target": "/dev/sda", "admin_username": "v"}
        body.update(over)
        return self.client.post("/api/v1/reprovision/profiles/", body,
                                content_type="application/json")

    def test_no_key_and_no_password_is_refused(self):
        self.assertEqual(self._post().status_code, 400)

    def test_ssh_key_alone_is_enough(self):
        resp = self._post(ssh_authorized_keys="ssh-ed25519 AAAA test")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_password_alone_is_enough(self):
        resp = self._post(admin_password="$6$rounds=5000$abc$def")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_static_networking_needs_an_address_and_gateway(self):
        resp = self._post(ssh_authorized_keys="ssh-ed25519 AAAA test",
                          network_mode="static")
        self.assertEqual(resp.status_code, 400)

    def test_password_is_never_echoed_back(self):
        resp = self._post(admin_password="$6$rounds=5000$abc$def")
        self.assertNotIn("admin_password", resp.json())
        self.assertNotIn("admin_password_encrypted", resp.json())


class PreflightEndpointTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="pf-01", agent_token=f"tok-{uuid.uuid4()}",
            status=Host.Status.ONLINE)

    def _login(self, role):
        user = User.objects.create_user(username=f"pf-{role}", password="pw")
        UserProfile.objects.create(user=user, role=role)
        self.client.force_login(user)
        return user

    def _post(self, **over):
        body = {"host": str(self.host.id)}
        body.update(over)
        return self.client.post("/api/v1/reprovision/preflight/", body,
                                content_type="application/json")

    def test_dispatches_a_low_risk_probe(self):
        from apps.tasks.models import Task

        self._login(Role.ADMIN)
        resp = self._post(disk_target="/dev/sda")
        self.assertEqual(resp.status_code, 202)
        task = Task.objects.get(host=self.host, action="reprovision_preflight")
        self.assertEqual(task.risk_level, Task.RiskLevel.LOW)
        self.assertEqual(task.params["disk_target"], "/dev/sda")

    def test_viewer_may_check_readiness(self):
        # Read-only, so it needs only the view capability — an operator can
        # survey a fleet without arming anything.
        self._login(Role.VIEWER)
        self.assertEqual(self._post().status_code, 202)

    def test_anonymous_is_refused(self):
        self.assertIn(self._post().status_code, (401, 403))

    def test_previous_result_is_returned_when_available(self):
        import json

        from django.utils.timezone import now as _now

        from apps.tasks.models import Task

        Task.objects.get_or_create(
            host=self.host, action="reprovision_preflight",
            defaults=dict(nonce=uuid.uuid4().hex,
                          state=Task.State.COMPLETED,
                          completed_at=_now(),
                          result_output=json.dumps(
                              {"ok": True, "disks": [{"name": "/dev/sda"}]})))
        self._login(Role.ADMIN)
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
