"""Golden-file tests for the answer-file renderers.

A renderer regression produces a machine that wipes itself and then hangs at
an interactive prompt. These are pinned byte-for-byte on purpose: if you
change a renderer, you must consciously re-bless the expected output.
"""
import uuid
from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob
from apps.reprovision.renderers import get_renderer

TESTDATA = Path(__file__).parent / "testdata"
BASE_URL = "https://vigil.example.com"
ENROLL_TOKEN = "enroll-token-fixture"
ANSWER_TOKEN = "answer-token-fixture"


def build_job(family: str) -> RebuildJob:
    image = OSImage.objects.create(
        name=f"{family} test", os_family=family, version="1.0",
        architecture="x86_64", sha256="c" * 64, status=OSImage.Status.READY,
    )
    profile = InstallProfile.objects.create(
        name="standard", image=image, disk_target="/dev/sda",
        partition_scheme="lvm", filesystem="ext4",
        network_mode=InstallProfile.NetworkMode.DHCP,
        timezone="America/New_York", locale="en_US.UTF-8", keyboard="us",
        admin_username="vigil", ssh_authorized_keys="ssh-ed25519 AAAAC3Nz test",
        extra_packages=["curl", "vim"],
    )
    # Hostname is pinned because the golden files render it; only the token
    # has to be unique, so tests that loop over families get a fresh host.
    host = Host.objects.create(
        hostname="web-01", agent_token=f"tok-{uuid.uuid4()}",
        status=Host.Status.ONLINE,
    )
    return RebuildJob.objects.create(
        host=host, image=image, profile=profile,
        deadline=now() + timedelta(minutes=60),
    )


class GoldenFileTests(TestCase):
    maxDiff = None

    def assert_matches_golden(self, family, filename, key):
        job = build_job(family)
        rendered = get_renderer(family).render(job, BASE_URL, ENROLL_TOKEN)
        expected = (TESTDATA / filename).read_text()
        self.assertEqual(rendered[key], expected)

    def test_ubuntu_user_data(self):
        self.assert_matches_golden(
            "ubuntu", "expected_ubuntu_user_data.yaml", "user-data")

    def test_debian_preseed(self):
        self.assert_matches_golden(
            "debian", "expected_debian_preseed.cfg", "preseed.cfg")

    def test_rhel_kickstart(self):
        self.assert_matches_golden(
            "rhel", "expected_rhel_kickstart.cfg", "kickstart.cfg")


class ContractTests(TestCase):
    def test_ubuntu_also_emits_meta_data(self):
        # subiquity's nocloud-net source needs both files present.
        job = build_job("ubuntu")
        rendered = get_renderer("ubuntu").render(job, BASE_URL, ENROLL_TOKEN)
        self.assertIn("meta-data", rendered)

    def test_ubuntu_user_data_is_valid_yaml(self):
        import yaml

        job = build_job("ubuntu")
        rendered = get_renderer("ubuntu").render(job, BASE_URL, ENROLL_TOKEN)
        parsed = yaml.safe_load(rendered["user-data"])
        self.assertEqual(parsed["autoinstall"]["version"], 1)
        self.assertIn("late-commands", parsed["autoinstall"])

    def test_every_renderer_embeds_the_enrolment_token(self):
        for family in ("ubuntu", "debian", "rhel"):
            job = build_job(family)
            rendered = get_renderer(family).render(job, BASE_URL, ENROLL_TOKEN)
            joined = "\n".join(rendered.values())
            self.assertIn(ENROLL_TOKEN, joined, family)
            self.assertIn(BASE_URL, joined, family)

    def test_every_renderer_appends_the_escape_hatch_verbatim(self):
        for family in ("ubuntu", "debian", "rhel"):
            job = build_job(family)
            job.profile.raw_append = "### SENTINEL-RAW-BLOCK ###"
            job.profile.save(update_fields=["raw_append"])
            rendered = get_renderer(family).render(job, BASE_URL, ENROLL_TOKEN)
            self.assertIn("### SENTINEL-RAW-BLOCK ###",
                          "\n".join(rendered.values()), family)

    def test_cmdline_points_at_the_answer_endpoint(self):
        for family in ("ubuntu", "debian", "rhel"):
            job = build_job(family)
            cmdline = get_renderer(family).kernel_cmdline(
                job, BASE_URL, ANSWER_TOKEN)
            self.assertIn(ANSWER_TOKEN, cmdline, family)

    def test_rhel_cmdline_points_at_the_install_tree(self):
        job = build_job("rhel")
        cmdline = get_renderer("rhel").kernel_cmdline(job, BASE_URL, ANSWER_TOKEN)
        self.assertIn(f"inst.repo={BASE_URL}/reprovision/tree/{job.image.id}/",
                      cmdline)

    def test_unknown_family_raises(self):
        with self.assertRaises(KeyError):
            get_renderer("plan9")

    def test_registry_covers_every_declared_family(self):
        # A family selectable on OSImage but unrenderable would fail at the
        # worst possible moment — after the disk is already being wiped.
        from apps.reprovision.renderers import _REGISTRY

        self.assertEqual(set(_REGISTRY),
                         {f.value for f in OSImage.Family})

    def test_static_network_appears_in_output(self):
        job = build_job("debian")
        job.profile.network_mode = InstallProfile.NetworkMode.STATIC
        job.profile.static_address = "10.0.0.50/24"
        job.profile.gateway = "10.0.0.1"
        job.profile.dns = "10.0.0.1"
        job.profile.save()
        rendered = get_renderer("debian").render(job, BASE_URL, ENROLL_TOKEN)
        self.assertIn("10.0.0.50", rendered["preseed.cfg"])

    def test_rhel_static_netmask_is_derived_from_the_prefix(self):
        job = build_job("rhel")
        job.profile.network_mode = InstallProfile.NetworkMode.STATIC
        job.profile.static_address = "10.0.0.50/24"
        job.profile.gateway = "10.0.0.1"
        job.profile.dns = "10.0.0.1"
        job.profile.save()
        rendered = get_renderer("rhel").render(job, BASE_URL, ENROLL_TOKEN)
        self.assertIn("--netmask=255.255.255.0", rendered["kickstart.cfg"])

    def test_debian_answers_the_prompts_that_otherwise_hang(self):
        """The partman confirmations are what stop d-i asking a human."""
        job = build_job("debian")
        rendered = get_renderer("debian").render(job, BASE_URL, ENROLL_TOKEN)
        body = rendered["preseed.cfg"]
        for required in ("partman/confirm boolean true",
                         "partman/confirm_nooverwrite boolean true",
                         "partman-partitioning/confirm_write_new_label boolean true"):
            self.assertIn(required, body)

    def test_rhel_sections_are_balanced(self):
        job = build_job("rhel")
        body = get_renderer("rhel").render(
            job, BASE_URL, ENROLL_TOKEN)["kickstart.cfg"]
        self.assertEqual(body.count("%end"), 2)
        self.assertIn("%packages", body)
        self.assertIn("%post", body)
        self.assertIn("\nreboot\n", body)


class InstallerContractTests(TestCase):
    """The enrolment command must match what install.sh actually accepts.

    It pointed at /api/v1/agent/install.sh (the app is mounted at /agent/)
    and passed VIGIL_SERVER_URL (the script bakes in its own VIGIL_SERVER).
    Every rebuilt machine would have 404'd on the installer and never
    enrolled — invisible until the first real rebuild.
    """

    def test_installer_url_matches_the_mounted_route(self):
        from django.urls import reverse

        job = build_job("debian")
        script = get_renderer("debian").render(
            job, BASE_URL, ENROLL_TOKEN)["preseed.cfg"]
        self.assertIn(f"{BASE_URL}{reverse('agent-install-script')}", script)

    def test_installer_receives_the_enrol_token_variable(self):
        job = build_job("debian")
        script = get_renderer("debian").render(
            job, BASE_URL, ENROLL_TOKEN)["preseed.cfg"]
        self.assertIn(f"VIGIL_ENROLL_TOKEN={ENROLL_TOKEN}", script)

    def test_install_script_implements_the_enrol_exchange(self):
        """install.sh must know about VIGIL_ENROLL_TOKEN, or the whole
        re-enrolment chain is a no-op."""
        from django.template.loader import render_to_string

        script = render_to_string("agent_install.sh", {"base_url": BASE_URL})
        self.assertIn("VIGIL_ENROLL_TOKEN", script)
        self.assertIn("/api/v1/reprovision/enroll", script)

    def test_install_script_fails_loudly_on_a_rejected_enrolment(self):
        from django.template.loader import render_to_string

        script = render_to_string("agent_install.sh", {"base_url": BASE_URL})
        self.assertIn("exit 1", script)
