import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.reprovision.completion import complete_if_rebuilding
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob

S = RebuildJob.State


class CompletionTests(TestCase):
    def setUp(self):
        image = OSImage.objects.create(
            name="U", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="a" * 64,
            status=OSImage.Status.READY)
        profile = InstallProfile.objects.create(
            name="std", image=image, disk_target="/dev/sda",
            admin_username="v")
        self.host = Host.objects.create(
            hostname="h1", agent_token=f"tok-{uuid.uuid4()}",
            status=Host.Status.ONLINE, tags=["prod", "rack-3"],
            maintenance_until=now() + timedelta(hours=1))
        self.job = RebuildJob.objects.create(
            host=self.host, image=image, profile=profile, state=S.ENROLLING,
            completion_tag="rebuilt:need config",
            deadline=now() + timedelta(minutes=60))

    def test_completes_and_applies_the_tag(self):
        complete_if_rebuilding(self.host)
        self.job.refresh_from_db()
        self.host.refresh_from_db()
        self.assertEqual(self.job.state, S.COMPLETED)
        self.assertIn("rebuilt:need config", self.host.tags)

    def test_pre_rebuild_operator_tags_survive(self):
        complete_if_rebuilding(self.host)
        self.host.refresh_from_db()
        self.assertIn("prod", self.host.tags)
        self.assertIn("rack-3", self.host.tags)

    def test_maintenance_window_is_cleared(self):
        complete_if_rebuilding(self.host)
        self.host.refresh_from_db()
        self.assertIsNone(self.host.maintenance_until)

    def test_no_job_is_a_no_op(self):
        other = Host.objects.create(
            hostname="h2", agent_token=f"tok-{uuid.uuid4()}",
            status=Host.Status.ONLINE)
        self.assertIsNone(complete_if_rebuilding(other))

    def test_terminal_job_is_not_recompleted(self):
        self.job.state = S.COMPLETED
        self.job.save(update_fields=["state"])
        self.assertIsNone(complete_if_rebuilding(self.host))

    def test_empty_tag_is_skipped(self):
        self.job.completion_tag = ""
        self.job.save(update_fields=["completion_tag"])
        complete_if_rebuilding(self.host)
        self.host.refresh_from_db()
        self.assertEqual(sorted(self.host.tags), ["prod", "rack-3"])

    def test_reserved_namespace_is_refused_here_too(self):
        # Belt and braces: the API rejects it, but this writes straight to
        # the host, so it must not depend on that check having run.
        self.job.completion_tag = "agent:sneaky"
        self.job.save(update_fields=["completion_tag"])
        complete_if_rebuilding(self.host)
        self.host.refresh_from_db()
        self.assertNotIn("agent:sneaky", self.host.tags)

    def test_duplicate_tag_is_not_added_twice(self):
        self.host.tags = ["prod", "rebuilt:need config"]
        self.host.save(update_fields=["tags"])
        complete_if_rebuilding(self.host)
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags.count("rebuilt:need config"), 1)

    def test_job_still_completes_when_the_baseline_is_broken(self):
        """A broken baseline must not strand the job in ENROLLING."""
        from unittest.mock import patch

        from apps.baselines.models import Baseline

        self.job.post_baseline = Baseline.objects.create(name="b", enabled=True)
        self.job.save(update_fields=["post_baseline"])
        with patch("apps.baselines.models.dispatch_to_host",
                   side_effect=RuntimeError("boom")):
            complete_if_rebuilding(self.host)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, S.COMPLETED)


class CheckinIntegrationTests(TestCase):
    def test_checkin_from_a_rebuilt_agent_completes_the_job(self):
        image = OSImage.objects.create(
            name="U", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="b" * 64,
            status=OSImage.Status.READY)
        profile = InstallProfile.objects.create(
            name="std", image=image, disk_target="/dev/sda",
            admin_username="v")
        host = Host.objects.create(
            hostname="h9", agent_token="fresh-token",
            status=Host.Status.ONLINE)
        job = RebuildJob.objects.create(
            host=host, image=image, profile=profile, state=S.ENROLLING,
            completion_tag="rebuilt:need config",
            deadline=now() + timedelta(minutes=60))

        self.client.post(
            "/api/v1/checkin", {"hostname": "h9", "metrics": {}},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer fresh-token")

        job.refresh_from_db()
        host.refresh_from_db()
        self.assertEqual(job.state, S.COMPLETED)
        self.assertIn("rebuilt:need config", host.tags)


class ProfileCompletionTagTests(TestCase):
    """A profile's standing tags land on the host when the rebuild finishes.

    RebuildJob.completion_tag is the one-off typed into a single ceremony;
    the profile's completion_tags is the standing version, so "everything
    built from this profile is role:web" is stated once instead of retyped
    per rebuild.
    """

    def _setup(self, *, profile_tags=None, job_tag="", host_tags=None):
        from apps.reprovision.completion import complete_if_rebuilding

        image = OSImage.objects.create(
            name="Ubuntu", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="a" * 64,
            status=OSImage.Status.READY)
        profile = InstallProfile.objects.create(
            name="p", image=image, disk_target="/dev/sda",
            admin_username="admin", ssh_authorized_keys="ssh-ed25519 AAAA x",
            completion_tags=profile_tags or [])
        host = Host.objects.create(
            hostname="rebuilt", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
            tags=host_tags or [])
        RebuildJob.objects.create(
            host=host, image=image, profile=profile,
            state=RebuildJob.State.ENROLLING, completion_tag=job_tag,
            deadline=now() + timedelta(minutes=60))
        complete_if_rebuilding(host)
        host.refresh_from_db()
        return host

    def test_profile_tags_are_applied_on_completion(self):
        host = self._setup(profile_tags=["role:web", "env:lab"])
        self.assertEqual(host.tags, ["role:web", "env:lab"])

    def test_the_job_tag_still_applies(self):
        """The per-rebuild one-off keeps working alongside the profile's."""
        host = self._setup(profile_tags=["role:web"], job_tag="batch:2026-08")
        self.assertEqual(host.tags, ["role:web", "batch:2026-08"])

    def test_existing_host_tags_are_kept(self):
        host = self._setup(profile_tags=["role:web"], host_tags=["site:hq"])
        self.assertEqual(host.tags, ["site:hq", "role:web"])

    def test_a_tag_the_host_already_has_is_not_duplicated(self):
        host = self._setup(profile_tags=["role:web"], host_tags=["role:web"])
        self.assertEqual(host.tags, ["role:web"])

    def test_a_tag_repeated_across_profile_and_job_lands_once(self):
        host = self._setup(profile_tags=["role:web"], job_tag="role:web")
        self.assertEqual(host.tags, ["role:web"])

    def test_the_reserved_agent_namespace_is_never_written(self):
        """A rogue agent must not be able to impersonate an operator tag, and
        this path writes straight to the host without going through the API."""
        host = self._setup(profile_tags=["agent:linux", "role:web"])
        self.assertEqual(host.tags, ["role:web"])

    def test_blank_and_whitespace_tags_are_ignored(self):
        host = self._setup(profile_tags=["", "   ", "role:web"])
        self.assertEqual(host.tags, ["role:web"])

    def test_a_profile_with_no_tags_leaves_the_host_alone(self):
        host = self._setup(host_tags=["site:hq"])
        self.assertEqual(host.tags, ["site:hq"])
