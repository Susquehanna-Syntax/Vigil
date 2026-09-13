"""The uninstallers must remove what the installers create, and nothing wider.

There was no uninstall path on any platform, so removing an agent meant
knowing by heart which service, binary, config and state directory to delete —
and the self-update leaves staged directories beside the install that nobody
would think to look for.

These tests pin two things: that every path an installer creates is accounted
for, and that no recursive delete interpolates a variable. An uninstaller runs
as root or administrator, and an empty variable in an `rm -rf` is how people
lose machines.
"""

import re

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse

BASE_URL = "https://vigil.example.com"


class UninstallersRemoveWhatInstallersCreate(SimpleTestCase):
    def setUp(self):
        self.install_sh = render_to_string("agent_install.sh", {"base_url": BASE_URL})
        self.install_ps1 = render_to_string("agent_install.ps1", {"base_url": BASE_URL})
        self.sh = render_to_string("agent_uninstall.sh", {"base_url": BASE_URL})
        self.ps1 = render_to_string("agent_uninstall.ps1", {"base_url": BASE_URL})

    def test_shell_uninstaller_covers_every_path_the_installer_writes(self):
        for path in ("/etc/systemd/system/vigil-agent.service",
                     "/usr/local/bin/vigil-agent",
                     "/var/lib/vigil-agent",
                     "/etc/vigil/agent.yml",
                     "com.susquehannasyntax.vigil-agent.plist"):
            self.assertIn(path, self.install_sh, f"test is stale: {path}")
            self.assertIn(path, self.sh,
                          f"install.sh creates {path} and uninstall.sh never "
                          f"removes it")

    def test_shell_uninstaller_removes_the_service_account(self):
        self.assertIn("useradd", self.install_sh)
        self.assertIn("userdel", self.sh)

    def test_powershell_uninstaller_covers_the_install_locations(self):
        for path in (r"C:\Program Files\Vigil", r"C:\ProgramData\Vigil"):
            self.assertIn(path, self.ps1)
        self.assertIn("sc.exe delete", self.ps1)

    def test_powershell_uninstaller_clears_self_update_leftovers(self):
        """A half-finished self-update stages beside the install."""
        for stray in (".new", ".old", "vigil-agent-update.cmd"):
            self.assertIn(stray, self.ps1,
                          "an interrupted self-update leaves this behind and "
                          "an uninstall would strand it")

    def test_no_recursive_delete_interpolates_a_variable(self):
        """Literal paths only, in a script that runs as root."""
        offenders = [
            line.strip() for line in self.sh.splitlines()
            if re.search(r"rm\s+-[rf]*r[rf]*\s+.*\$", line)
        ]
        self.assertEqual(offenders, [], offenders)

    def test_both_stop_the_service_before_deleting_its_files(self):
        self.assertIn("systemctl stop", self.sh)
        self.assertIn("Stop-Service", self.ps1)
        self.assertIn("Stopped", self.ps1,
                      "deleting while the service still holds its files "
                      "leaves a half-removed install")

    def test_config_can_be_kept(self):
        self.assertIn("--keep-config", self.sh)
        self.assertIn("VIGIL_KEEP_CONFIG", self.ps1)

    def test_state_is_always_removed(self):
        """A re-install must not inherit a stale server key pin."""
        self.assertIn("/var/lib/vigil-agent", self.sh)
        self.assertIn("DataDir", self.ps1)


class UninstallEndpoints(SimpleTestCase):
    def test_shell_uninstaller_is_served(self):
        resp = self.client.get(reverse("agent-uninstall-script"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Vigil agent uninstaller", resp.content)

    def test_powershell_uninstaller_is_served(self):
        resp = self.client.get(reverse("agent-uninstall-ps1"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"RunAsAdministrator", resp.content)

    def test_they_do_not_require_authentication(self):
        """Same reasoning as the installer: an agent being removed may no
        longer have working credentials."""
        for name in ("agent-uninstall-script", "agent-uninstall-ps1"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
