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


class WindowsConfigTreeLockedTests(SimpleTestCase):
    """SEC-2: C:\\ProgramData's default ACL let a standard user create the scripts
    folder the agent runs scripts from as LocalSystem (proven on the Windows VM).
    The installer takes the tree back and locks it: fail closed, never following a
    link a user planted (both checked live on the VM)."""

    def setUp(self):
        self.ps1 = render_to_string("agent_install.ps1", {"base_url": BASE_URL, "public_key": "K"})

    def _at(self, needle):
        self.assertIn(needle, self.ps1)
        return self.ps1.index(needle)

    def test_top_folder_is_locked_first_by_sid_without_following_links(self):
        take = self._at('Invoke-AclStep $ConfigDir /setowner $Admins /L /C')
        lock = self._at('Invoke-AclStep $ConfigDir /inheritance:r /grant "*S-1-5-18:(OI)(CI)(F)" '
                        '/grant "$($Admins):(OI)(CI)(F)" /grant "*S-1-5-32-545:(RX)" /L')
        self.assertLess(take, lock)

    def test_links_are_removed_before_anything_recurses(self):
        remove = self._at("Remove-PlantedLinks $ConfigDir\n")
        own = self._at('Invoke-AclStep (Join-Path $ConfigDir "*") /setowner $Admins /T /L /C')
        reset = self._at('Invoke-AclStep (Join-Path $ConfigDir "*") /reset /T /L /C')
        self.assertLess(self._at('Invoke-AclStep $ConfigDir /inheritance:r'), remove)
        self.assertLess(remove, own)
        self.assertLess(own, reset)
        self.assertLess(reset, self._at("icacls.exe $ConfigPath /inheritance:r"),
                        "agent.yml's own lock must come after the reset, or the reset undoes it")

    def test_a_planted_config_folder_link_is_replaced_before_use(self):
        check = self._at("if (Test-Link $ConfigDir)")
        self.assertLess(check, self._at("New-Item -ItemType Directory -Force -Path $ConfigDir"))
        self.assertLess(check, self._at("New-Item -ItemType Directory -Force -Path $DataDir"))

    def test_every_lock_step_fails_closed(self):
        self.assertIn("if ($LASTEXITCODE -ne 0) {", self.ps1)
        self.assertIn("throw \"Could not secure", self.ps1)
        block = self.ps1[self.ps1.index("function Invoke-AclStep"):self.ps1.index("New-Item -ItemType Directory -Force -Path $DataDir")]
        self.assertNotIn("& icacls.exe $ConfigDir", block, "lock steps go through Invoke-AclStep")

    def test_scripts_folder_is_created_after_the_lock(self):
        self.assertLess(self._at('Invoke-AclStep $ConfigDir /inheritance:r'),
                        self._at("New-Item -ItemType Directory -Force -Path $ScriptsDir"))

    def test_monitor_service_can_still_write_its_log(self):
        self.assertIn('icacls.exe $LogPath /grant "$($ServiceAccount):(M)"', self.ps1)

    def test_server_key_is_written_into_agent_yml(self):
        self.assertIn('$ServerPublicKey = "K"', self.ps1)
        self.assertIn("server_public_key:", self.ps1)
