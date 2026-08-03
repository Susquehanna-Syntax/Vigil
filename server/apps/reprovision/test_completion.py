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
