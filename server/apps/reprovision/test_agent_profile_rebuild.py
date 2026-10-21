"""An install profile that names an agent profile ships its config token (QA-62).

The other half of the guarantee is that nothing changes when it does not: the
golden-file renderer tests pin the old bytes, and one test here asserts the
answer file carries no VIGIL_CONFIG_TOKEN at all.
"""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.accounts.models import Role, UserProfile
from apps.agentprofiles.models import AgentProfile, EnrollConfig
from apps.hosts.models import Host

from . import jobs
from .models import InstallProfile, OSImage, RebuildJob
from .renderers import get_renderer

BASE_URL = "https://vigil.example.com"
ENROLL_TOKEN = "enroll-token-fixture"


def build(family: str, agent_profile=None):
    image = OSImage.objects.create(
        name=f"{family} test", os_family=family, version="1.0",
        architecture="x86_64", sha256="c" * 64, status=OSImage.Status.READY)
    profile = InstallProfile.objects.create(
        name="standard", image=image, disk_target="/dev/sda",
        admin_username="vigil", ssh_authorized_keys="ssh-ed25519 AAAAC3Nz test",
        agent_profile=agent_profile)
    host = Host.objects.create(
        hostname="web-01", agent_token=f"tok-{uuid4()}",
        status=Host.Status.ONLINE)
    job = RebuildJob.objects.create(
        host=host, image=image, profile=profile,
        state=RebuildJob.State.REBOOTING, deadline=now().replace(microsecond=0))
    return job, profile


class RendererTokenTests(TestCase):
    def test_late_command_carries_both_tokens(self):
        job, _ = build("ubuntu", AgentProfile.objects.create(
            name="ops", mode="managed", allowlist=["restart_service"]))
        rendered = get_renderer("ubuntu").render(
            job, BASE_URL, ENROLL_TOKEN, config_token="cfg-token-fixture")
        late = rendered["user-data"]
        assert "VIGIL_CONFIG_TOKEN=cfg-token-fixture" in late
        assert "VIGIL_ENROLL_TOKEN=enroll-token-fixture" in late
        # The config token is the installer's first environment variable, so
        # it sits next to the enrolment one rather than replacing it.
        assert late.index("VIGIL_CONFIG_TOKEN") < late.index("VIGIL_ENROLL_TOKEN")

    def test_no_agent_profile_renders_the_old_bytes(self):
        job, _ = build("ubuntu")
        without = get_renderer("ubuntu").render(job, BASE_URL, ENROLL_TOKEN)
        with_blank = get_renderer("ubuntu").render(
            job, BASE_URL, ENROLL_TOKEN, config_token="")
        self.assertEqual(without, with_blank)
        self.assertNotIn("VIGIL_CONFIG_TOKEN", without["user-data"])

    def test_every_family_takes_the_token(self):
        for family, key in (("ubuntu", "user-data"), ("debian", "preseed.cfg"),
                            ("rhel", "kickstart.cfg")):
            job, _ = build(family)
            body = get_renderer(family).render(
                job, BASE_URL, ENROLL_TOKEN, config_token="cfg")[key]
            self.assertIn("VIGIL_CONFIG_TOKEN=cfg VIGIL_ENROLL_TOKEN=", body)


class RebuildMintsAConfigTokenTests(TestCase):
    """The whole path: ceremony to answer file to redeemed agent.yml."""

    def setUp(self):
        self.image = OSImage.objects.create(
            name="Ubuntu", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="e" * 64, status=OSImage.Status.READY)
        self.host = Host.objects.create(hostname="web-01", agent_token="t" * 32,
                                        status=Host.Status.ONLINE)
        patcher = patch("apps.accounts.totp.require_totp_confirmation",
                        return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_user(
            username="admin", password="correct-horse-battery")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)

    def _request_job(self, profile):
        resp = self.client.post("/api/v1/reprovision/jobs/", {
            "host": str(self.host.id),
            "image": str(self.image.id),
            "profile": str(profile.id),
            "password": "correct-horse-battery",
            "totp": "123456",
            "typed_hostname": "web-01",
            "acknowledge_plaintext_transport": True,
        }, content_type="application/json", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(resp.status_code, 201, resp.content)
        return RebuildJob.objects.get(pk=resp.json()["id"])

    def test_rebuild_with_an_agent_profile_offers_a_redeemable_config(self):
        agent_profile = AgentProfile.objects.create(
            name="ops", mode="managed", allowlist=["restart_service"],
            tags=["role:web"])
        profile = InstallProfile.objects.create(
            name="std", image=self.image, disk_target="/dev/sda",
            admin_username="v", ssh_authorized_keys="ssh-ed25519 AAAA k",
            deadline_minutes=30, agent_profile=agent_profile)

        job = self._request_job(profile)

        config_token = jobs.pop_config_token(job)
        assert config_token
        config = EnrollConfig.objects.get(profile=agent_profile)
        # Long enough to outlive a rebuild that overruns its deadline.
        assert config.expires_at >= job.deadline

        _answer, enroll = jobs.mint_tokens(job)
        jobs.stash_enroll_token(job, enroll)
        self.client.get(f"/reprovision/answer/{_answer}/user-data")
        # And the token the installer was handed really does redeem.
        resp = self.client.get(f"/agent/enroll-config/{config_token}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        assert 'agent_token: "REPLACE_WITH_TOKEN"' in body
        assert "mode: managed" in body
        assert "- restart_service" in body
        assert "- role:web" in body

    def test_rebuild_without_an_agent_profile_stashes_nothing(self):
        profile = InstallProfile.objects.create(
            name="std", image=self.image, disk_target="/dev/sda",
            admin_username="v", ssh_authorized_keys="ssh-ed25519 AAAA k")

        job = self._request_job(profile)

        self.assertEqual(jobs.pop_config_token(job), "")
        self.assertEqual(EnrollConfig.objects.count(), 0)
        _answer, enroll = jobs.mint_tokens(job)
        jobs.stash_enroll_token(job, enroll)
        user_data = self.client.get(
            f"/reprovision/answer/{_answer}/user-data").content.decode()
        assert "VIGIL_CONFIG_TOKEN" not in user_data
