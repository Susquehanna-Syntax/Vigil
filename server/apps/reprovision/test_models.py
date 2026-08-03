from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob
from apps.tasks.models import TaskRun


def make_image(**kw):
    defaults = dict(
        name="Ubuntu 24.04", os_family=OSImage.Family.UBUNTU, version="24.04",
        architecture="x86_64", sha256="a" * 64, status=OSImage.Status.READY,
    )
    defaults.update(kw)
    return OSImage.objects.create(**defaults)


def make_host(hostname="web-01"):
    return Host.objects.create(
        hostname=hostname, agent_token=f"tok-{hostname}",
        status=Host.Status.ONLINE,
    )


class ModelTests(TestCase):
    def test_image_defaults_to_importing(self):
        image = OSImage.objects.create(
            name="Rocky 9", os_family=OSImage.Family.RHEL, version="9",
            architecture="x86_64", sha256="b" * 64,
        )
        self.assertEqual(image.status, OSImage.Status.IMPORTING)

    def test_profile_defaults_to_dhcp(self):
        profile = InstallProfile.objects.create(
            name="standard", image=make_image(), disk_target="/dev/sda",
            admin_username="admin",
        )
        self.assertEqual(profile.network_mode, InstallProfile.NetworkMode.DHCP)
        self.assertEqual(profile.deadline_minutes, 60)

    def test_job_starts_pending_with_no_tokens_consumed(self):
        image = make_image()
        profile = InstallProfile.objects.create(
            name="standard", image=image, disk_target="/dev/sda",
            admin_username="admin",
        )
        job = RebuildJob.objects.create(
            host=make_host(), image=image, profile=profile,
            deadline=now() + timedelta(minutes=60),
        )
        self.assertEqual(job.state, RebuildJob.State.PENDING)
        self.assertIsNone(job.enroll_consumed_at)
        self.assertIsNone(job.answer_fetched_at)
        self.assertFalse(job.is_terminal)

    def test_terminal_states_are_the_four_endings(self):
        self.assertEqual(
            RebuildJob.TERMINAL_STATES,
            frozenset({"completed", "failed", "aborted", "timed_out"}),
        )

    def test_answer_is_servable_only_across_the_point_of_no_return(self):
        self.assertEqual(
            RebuildJob.ANSWER_SERVABLE_STATES,
            frozenset({"rebooting", "installing"}),
        )

    def test_host_maintenance_until_defaults_null(self):
        self.assertIsNone(make_host().maintenance_until)

    def test_taskrun_has_a_reprovision_source(self):
        self.assertEqual(TaskRun.Source.REPROVISION, "reprovision")

    def test_image_is_protected_while_a_job_references_it(self):
        """Deleting an image mid-rebuild would strand the installer."""
        from django.db.models import ProtectedError

        image = make_image()
        profile = InstallProfile.objects.create(
            name="standard", image=image, disk_target="/dev/sda",
            admin_username="admin",
        )
        RebuildJob.objects.create(
            host=make_host(), image=image, profile=profile,
            deadline=now() + timedelta(minutes=60),
        )
        with self.assertRaises(ProtectedError):
            image.delete()
