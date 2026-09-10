import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

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

    def _create(self, remote_addr="127.0.0.1", **over):
        body = {
            "host": str(self.host.id), "image": str(self.image.id),
            "profile": str(self.profile.id), "password": "pw",
            "totp": "123456", "typed_hostname": "web-01",
            "acknowledge_plaintext_transport": True,
        }
        body.update(over)
        # REMOTE_ADDR matters now: the transport gate reads the socket peer,
        # so a test that wants the gate to fire has to come from a public one.
        return self.client.post("/api/v1/reprovision/jobs/", body,
                                content_type="application/json",
                                REMOTE_ADDR=remote_addr)

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
    def test_a_job_no_longer_takes_a_tag_or_a_playbook(self, _totp):
        # Both moved off the ceremony: the install profile carries
        # completion_tags, and a playbook that should run on a rebuilt machine
        # is auto-enroll's job. Anything still sending them is ignored rather
        # than refused, so an older client does not start failing.
        self._login(Role.ADMIN)
        resp = self._create(completion_tag="agent:rebuilt",
                            post_playbook="00000000-0000-0000-0000-000000000000")
        self.assertEqual(resp.status_code, 201)
        job = RebuildJob.objects.get(pk=resp.json()["id"])
        self.assertEqual(job.completion_tag, "")
        self.assertIsNone(job.post_playbook)

    def test_the_reserved_tag_namespace_is_still_refused_on_the_profile(self):
        # The guard the job used to carry now lives where tags are actually
        # set — losing it with the field would have been the real regression.
        self._login(Role.ADMIN)
        resp = self.client.post("/api/v1/reprovision/profiles/", {
            "name": "reserved", "image": str(self.image.id),
            "disk_target": "/dev/sda", "admin_password_hash": "$6$x$y",
            "completion_tags": ["agent:rebuilt"],
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400, resp.content)

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
        """From a public peer, over plain HTTP, without the acknowledgement."""
        self._login(Role.ADMIN)
        resp = self._create(remote_addr="93.184.216.34",
                            acknowledge_plaintext_transport=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get("code"), "plaintext_transport")

    @patch("apps.accounts.totp.require_totp_confirmation", return_value=None)
    def test_a_lan_peer_needs_no_acknowledgement(self, _totp):
        """The case the gate exists to permit: a homelab rebuild over the LAN,
        where there is no certificate to be had and the installer is on the
        same wire."""
        self._login(Role.ADMIN)
        resp = self._create(remote_addr="192.168.1.40",
                            acknowledge_plaintext_transport=False)
        self.assertNotEqual(resp.status_code, 400)

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


class PlainHttpOnALanTests(TestCase):
    """A self-hosted Vigil is normally reached at http://10.x on the LAN, with
    no certificate to be had. Refusing the rebuild there blocked the feature
    outright rather than protecting anything.

    The question is answered from the socket peer, not the Host header — see
    test_the_host_header_cannot_forge_a_private_network below for why.
    """

    def _refused(self, peer):
        from apps.reprovision.views import _served_on_a_private_network

        class _Req:
            META = {"REMOTE_ADDR": peer}

            def get_host(self):
                # Deliberately a public name: nothing may read this.
                return "vigil.example.com"

        return not _served_on_a_private_network(_Req())

    def test_rfc1918_addresses_are_treated_as_private(self):
        for peer in ("10.0.0.109", "192.168.1.50", "172.16.4.9"):
            self.assertFalse(self._refused(peer), peer)

    def test_loopback_is_private(self):
        self.assertFalse(self._refused("127.0.0.1"))
        self.assertFalse(self._refused("::1"))

    def test_a_public_peer_still_requires_the_acknowledgement(self):
        for peer in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(self._refused(peer), peer)

    def test_the_host_header_cannot_forge_a_private_network(self):
        """The bypass this gate had.

        `_served_on_a_private_network` used to read `request.get_host()`, which
        is the client's own Host header, checked only against ALLOWED_HOSTS —
        and ALLOWED_HOSTS keeps its default ["localhost", "127.0.0.1"] even
        after VIGIL_PUBLIC_URL appends to it. So on an internet-facing Vigil
        configured exactly as documented, `Host: 127.0.0.1` read as private and
        the answer file went out over plain HTTP with nothing recorded.
        """
        from apps.reprovision.views import _served_on_a_private_network

        class _SpoofedReq:
            META = {"REMOTE_ADDR": "93.184.216.34"}   # a public peer

            def get_host(self):
                return "127.0.0.1"                    # the old bypass

        self.assertFalse(_served_on_a_private_network(_SpoofedReq()))

    def test_an_ipv6_mapped_ipv4_peer_is_read_as_that_ipv4(self):
        """::ffff:127.0.0.1 is loopback, and is the classic way past a check
        that only looks at the textual form."""
        self.assertFalse(self._refused("::ffff:127.0.0.1"))
        self.assertTrue(self._refused("::ffff:93.184.216.34"))

    def test_the_override_setting_demands_it_everywhere(self):
        with override_settings(VIGIL_REQUIRE_HTTPS_FOR_REBUILD=True):
            self.assertTrue(self._refused("10.0.0.109:8000"))
