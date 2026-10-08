"""SEC-3: install.sh writes the server's key into agent.yml, keeps agent.yml
root-owned, and does not believe a task-running mode from a file the
unprivileged service account could have written. The block is run for real,
in bash, against a scratch copy of agent.yml."""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

KEY = "q1Ivm7G1oQ0kq5mH0q2N4u3mXyZ9b8F6c5d4e3f2a1A="
START = "# ── Server key and mode (SEC-3)"
END = "# ── Service installation"


class InstallShSec3Tests(SimpleTestCase):
    def setUp(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": KEY})
        self.block = script[script.index(START):script.index(END)]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "agent.yml"

    def _run(self, config, env=None):
        self.cfg.write_text(config)
        body = self.block.replace("/etc/vigil/agent.yml", str(self.cfg))
        # chown/chmod to root are the real installer's job; here the file is ours.
        harness = "chown() { :; }\nchmod() { :; }\n" + body + '\necho "MODE=$CFG_MODE"\n'
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                                env={**os.environ, **(env or {})}, timeout=30)
        return result, self.cfg.read_text()

    def test_key_is_written_once_and_replaced_on_reinstall(self):
        _, text = self._run('server_url: "x"\nmode: monitor\n')
        self.assertEqual(text.count("server_public_key:"), 1)
        self.assertIn(f'server_public_key: "{KEY}"', text)
        _, text = self._run(text.replace(KEY, "OLDKEY"))
        self.assertEqual(text.count("server_public_key:"), 1)
        self.assertIn(KEY, text)

    def test_untrusted_file_cannot_ask_for_root(self):
        if os.geteuid() == 0:
            self.skipTest("the scratch file would be root-owned")
        result, text = self._run('server_url: "x"\nmode: full_control\n')
        self.assertIn("MODE=monitor", result.stdout)
        self.assertIn("not trusted to grant root", result.stderr)
        self.assertRegex(text, r"(?m)^mode: monitor$")

    def test_vigil_mode_confirms_the_mode(self):
        result, text = self._run('server_url: "x"\nmode: full_control\n', {"VIGIL_MODE": "managed"})
        self.assertIn("MODE=managed", result.stdout)
        self.assertRegex(text, r"(?m)^mode: managed$")

    def test_vigil_mode_is_validated(self):
        result, _ = self._run('server_url: "x"\n', {"VIGIL_MODE": "root"})
        self.assertNotEqual(result.returncode, 0)

    def test_monitor_config_is_root_owned_and_group_readable(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": KEY})
        self.assertIn("chown root:vigil-agent /etc/vigil/agent.yml", script)
        self.assertIn("chmod 640 /etc/vigil/agent.yml", script)
        self.assertNotIn("chown vigil-agent /etc/vigil/agent.yml", script)
        self.assertIsNone(re.search(r"AGENT_MODE=\"\$\(sed", script), "mode must come from the trusted CFG_MODE")
