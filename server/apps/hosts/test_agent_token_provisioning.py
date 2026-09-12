"""The installers must not ship a working credential.

`register()` stores whatever token an agent presents; the server never issues
one. So the literal `REPLACE_WITH_TOKEN` the installers used to write into
agent.yml was a real, working credential — published in the install script, on
the Settings page, and in this repository.

Found by running it: an agent installed and started without editing its config
registered happily, went `online`, and answered `Bearer REPLACE_WITH_TOKEN`
with the server's signing public key and its task queue. Worse, because
registration is idempotent on the token, a second machine presenting the same
placeholder got back the first host's id with `status: online` — inheriting an
approved identity with no admin action, which is precisely what the enrollment
ceremony exists to prevent.
"""

from django.template.loader import render_to_string
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.hosts.views import PLACEHOLDER_AGENT_TOKENS

BASE_URL = "https://vigil.example.com"


class InstallersGenerateATokenRatherThanAPlaceholder(TestCase):
    def setUp(self):
        # Strip comments before scanning. This is the fifth guard in this
        # codebase to flag the prose documenting it — the comment beside the
        # RNG says "not Get-Random" and explains why, which a raw text scan
        # reads as the offence. Reuse the existing stripper rather than write
        # a sixth one.
        from apps.agent_dist.test_install_script_portability import (
            BothInstallersVerifyBeforeInstalling as _P,
        )
        self.sh = render_to_string("agent_install.sh", {"base_url": BASE_URL})
        self.ps1 = render_to_string("agent_install.ps1", {"base_url": BASE_URL})
        self.ps1_code = _P._strip_ps_comments(self.ps1)
        self.sh_code = "\n".join(
            ln for ln in self.sh.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_shell_installer_generates_a_random_token(self):
        self.assertIn("/dev/urandom", self.sh_code)
        # The reprovision branch has always generated one; the ordinary install
        # branch must too, so require more than a single occurrence.
        self.assertGreaterEqual(
            self.sh_code.count("/dev/urandom"), 2,
            "only one branch of install.sh generates a token; the default "
            "install path is still leaving the placeholder",
        )

    def test_powershell_installer_uses_a_crypto_rng(self):
        self.assertIn("RNGCryptoServiceProvider", self.ps1_code)
        self.assertNotIn(
            "Get-Random", self.ps1_code,
            "Get-Random is not a cryptographic RNG and this value is a credential",
        )

    def test_no_installer_tells_the_operator_to_set_a_token_by_hand(self):
        """The instruction only existed because the token was a placeholder."""
        for name, script in (("install.sh", self.sh_code), ("install.ps1", self.ps1_code)):
            self.assertNotRegex(
                script, r"set agent_token",
                f"{name} still instructs the operator to set a token that is "
                "now generated for them",
            )


class TheServerRefusesAPlaceholderToken(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_registration_with_the_placeholder_is_refused(self):
        resp = self.client.post(
            "/api/v1/register",
            {"agent_token": "REPLACE_WITH_TOKEN", "hostname": "box-1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(Host.objects.count(), 0)

    def test_a_second_machine_cannot_inherit_an_approved_identity(self):
        """The bypass this guard exists to close.

        Registration is idempotent on the token, so before the guard a second
        machine presenting the placeholder was handed the first host's id and
        its `online` status.
        """
        host = Host.objects.create(
            hostname="real-box",
            agent_token="REPLACE_WITH_TOKEN",
            status=Host.Status.ONLINE,
        )
        resp = self.client.post(
            "/api/v1/register",
            {"agent_token": "REPLACE_WITH_TOKEN", "hostname": "attacker-laptop"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("id", resp.data)
        host.refresh_from_db()
        self.assertEqual(host.hostname, "real-box")

    def test_a_generated_token_still_registers_normally(self):
        """The guard must not make enrolment harder for everyone else."""
        resp = self.client.post(
            "/api/v1/register",
            {"agent_token": "a" * 64, "hostname": "box-2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], Host.Status.PENDING)

    def test_an_existing_host_on_a_placeholder_keeps_checking_in(self):
        """Monitoring must not stop because a credential is weak.

        CLAUDE.md treats a path that can break monitoring as severity one, so
        the guard is deliberately on registration only. Rotating the token is
        an operator action, not something to force by dropping ingest.
        """
        Host.objects.create(
            hostname="legacy-box",
            agent_token="REPLACE_WITH_TOKEN",
            status=Host.Status.ONLINE,
        )
        resp = self.client.post(
            "/api/v1/checkin", {}, format="json",
            HTTP_AUTHORIZATION="Bearer REPLACE_WITH_TOKEN",
        )
        self.assertEqual(resp.status_code, 200)

    def test_the_placeholder_set_matches_what_the_templates_once_wrote(self):
        self.assertIn("REPLACE_WITH_TOKEN", PLACEHOLDER_AGENT_TOKENS)
