"""Re-installing the agent on a machine that already has an agent.yml.

Phase 01 got the wizard's token onto a fresh install, but three bugs on a real
Ubuntu VM still stopped it from ever seeing the host:

  A. /etc/vigil was created under the installer's umask (0027 on Ubuntu 26.04),
     so the monitor-mode vigil-agent user could not traverse into it to read
     the agent.yml it owns — the agent crash-looped.
  B. VIGIL_TOKEN was only consulted inside `if [ ! -f /etc/vigil/agent.yml ]`,
     so on a re-install the wizard's token was silently dropped and the agent
     kept the first install's token, which the server no longer waits for.
  C. `systemctl start` is a no-op on an already-running unit, so even a
     corrected token would never have been picked up.

These tests pin the fixes against the rendered installer.
"""

import os
import subprocess
import tempfile

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class InstallerReinstall(SimpleTestCase):
    def setUp(self):
        self.sh = render_to_string(
            "agent_install.sh", {"base_url": "https://vigil.example.com"}
        )
        self.ps1 = render_to_string(
            "agent_install.ps1", {"base_url": "https://vigil.example.com"}
        )

    def _reinstall_sed_line(self):
        """The sed line that replaces agent_token in an existing config."""
        for line in self.sh.splitlines():
            if line.strip().startswith('sed -i.bak "s|^agent_token:'):
                return line
        self.fail("no reinstall sed line in install.sh")

    def _run_reinstall_sed(self, config_text, token="newtok"):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "agent.yml")
            with open(config, "w") as fh:
                fh.write(config_text)
            line = self._reinstall_sed_line().replace("/etc/vigil/agent.yml", config)
            proc = subprocess.run(
                ["bash", "-c", line],
                env={**os.environ, "VIGIL_TOKEN": token},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            with open(config) as fh:
                return proc, fh.read()

    def test_reinstall_replaces_the_token_in_an_existing_config(self):
        proc, content = self._run_reinstall_sed(
            'server_url: "http://old"\nagent_token: "oldtok"\nmode: managed\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('agent_token: "newtok"', content)
        self.assertNotIn("oldtok", content)

    def test_reinstall_leaves_other_config_lines_alone(self):
        proc, content = self._run_reinstall_sed(
            'server_url: "http://old"\nagent_token: "oldtok"\nmode: managed\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('server_url: "http://old"', content)
        self.assertIn("mode: managed", content)

    def test_config_dir_is_made_traversable(self):
        lines = self.sh.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "mkdir -p /etc/vigil":
                next_code = lines[i + 1].strip()
                while next_code.startswith("#"):
                    i += 1
                    next_code = lines[i + 1].strip()
                self.assertEqual(next_code, "chmod 0755 /etc/vigil")
                return
        self.fail("no `mkdir -p /etc/vigil` in install.sh")

    def test_a_running_agent_is_restarted(self):
        self.assertIn("systemctl restart vigil-agent", self.sh)
        for line in self.sh.splitlines():
            if "systemctl start vigil-agent" in line:
                self.assertIn("echo", line, f"bare systemctl start: {line.strip()}")

    def test_powershell_reinstall_replaces_the_token(self):
        self.assertIn("} elseif ($env:VIGIL_TOKEN) {", self.ps1)
        self.assertIn("-replace '(?m)^agent_token:[^\\r\\n]*'", self.ps1)

    def test_monitor_mode_joins_performance_monitor_users(self):
        grant = "Add-LocalGroupMember -SID S-1-5-32-558 -Member $ServiceAccount"
        self.assertIn(grant, self.ps1)
        self.assertLess(
            self.ps1.index(grant),
            self.ps1.index("Monitor mode: running the agent as the unprivileged"),
        )
