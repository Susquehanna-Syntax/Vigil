"""The Add Host wizard's Linux command must actually deliver the token.

It did not. `VIGIL_TOKEN=x curl ... | sudo bash` puts the variable in **curl's**
environment only. `bash` on the right of the pipe never sees it, and `sudo`
resets the environment anyway. `install.sh` then took its "no VIGIL_TOKEN"
branch and generated a random token, so `check_pending` never found the host
and the wizard waited forever.

Nothing caught it: the command rendered, the suite passed, and the failure
only appeared when a real host ran the real command. These tests are the cheap
half of that lesson. The expensive half is executing the command shape — so
`test_linux_command_delivers_token_to_the_installer` runs the substituted
command under bash with a fake download, and asserts the token arrives.
"""
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class EnrollCommandDeliversToken(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = (
            Path(settings.BASE_DIR) / "static" / "js" / "vigil-enroll.js"
        ).read_text()
        m = re.search(r"`([^`]*install\.sh[^`]*)`", cls.js)
        assert m, "no install.sh command template in vigil-enroll.js"
        cls.template = m.group(1)

    def test_linux_command_delivers_token_to_the_installer(self):
        cmd = self.template
        cmd = cmd.replace("${_enrollToken}", "tok123")
        cmd = cmd.replace("${origin}", "http://vigil.test")
        cmd = cmd.replace(
            "curl -fsSL http://vigil.test/agent/install.sh",
            "printf '%s' 'printf %s \"$VIGIL_TOKEN\"'",
        )
        cmd = cmd.replace("sudo ", "")
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(
            result.stdout, "tok123",
            f"the token did not reach the installer (stderr: {result.stderr})",
        )

    def test_linux_command_runs_the_installer_as_root(self):
        self.assertIn("| sudo ", self.template)

    def test_windows_command_sets_token_in_the_same_session(self):
        self.assertIn(
            '$env:VIGIL_TOKEN = "${_enrollToken}"; irm '
            "${origin}/agent/install.ps1 | iex",
            self.js,
        )
