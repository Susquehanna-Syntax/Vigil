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
START = "# VIGIL_TOKEN ends up inside sed replacements"
END = "# ── Service installation"


class InstallShSec3Tests(SimpleTestCase):
    def setUp(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": KEY})
        self.block = script[script.index(START):script.index(END)]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "agent.yml"

    def _run(self, config, env=None):
        if config is None:
            self.cfg.unlink(missing_ok=True)
        else:
            self.cfg.write_text(config)
            self.cfg.chmod(0o600)
        body = (self.block.replace("/etc/vigil/agent.yml", str(self.cfg))
                .replace("/etc/vigil", str(self.cfg.parent)))
        # chown/chmod to root are the real installer's job; here the file is ours.
        harness = ('VIGIL_SERVER="https://vigil.test"\nchown() { :; }\nchmod() { :; }\n'
                   + body + '\necho "MODE=$CFG_MODE"\n')
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                                env={**os.environ, **(env or {})}, timeout=30)
        return result, (self.cfg.read_text() if self.cfg.exists() else "")

    def test_fresh_install_gets_the_key_and_monitor_mode(self):
        result, text = self._run(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(text.count("server_public_key:"), 1)
        self.assertIn(f'server_public_key: "{KEY}"', text)
        self.assertRegex(text, r"(?m)^mode: monitor$")
        self.assertIn('server_url: "https://vigil.test"', text)

    def test_untrusted_config_is_set_aside_and_rebuilt(self):
        if os.geteuid() == 0:
            self.skipTest("the scratch file would be root-owned")
        planted = ('server_url: "https://evil.example"\nagent_token: "keepme-0123456789abcdef"\n'
                   'mode: full_control\n"mode": full_control\nallowlist: [run_command]\n')
        result, text = self._run(planted)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("none of its", result.stderr)
        self.assertNotIn("evil.example", text)
        self.assertNotIn("run_command", text)
        self.assertNotIn("full_control", text)
        self.assertIn("keepme-0123456789abcdef", text)
        self.assertRegex(text, r"(?m)^mode: monitor$")
        self.assertTrue(list(self.cfg.parent.glob("agent.yml.untrusted.*")))

    def test_an_untrusted_token_that_is_not_a_token_is_dropped(self):
        if os.geteuid() == 0:
            self.skipTest("the scratch file would be root-owned")
        result, text = self._run('server_url: "x"\nagent_token: "a|b&c;rm -rf /"\nmode: monitor\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rm -rf", text)
        self.assertIn("not a valid token", result.stderr)

    def test_vigil_mode_sets_the_mode(self):
        result, text = self._run(None, {"VIGIL_MODE": "managed"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(text, r"(?m)^mode: managed$")

    def test_vigil_mode_and_token_are_validated(self):
        self.assertNotEqual(self._run(None, {"VIGIL_MODE": "root"})[0].returncode, 0)
        self.assertNotEqual(self._run(None, {"VIGIL_TOKEN": "x|y"})[0].returncode, 0)

    def test_monitor_config_is_root_owned_and_group_readable(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": KEY})
        self.assertIn("chown root:vigil-agent /etc/vigil/agent.yml", script)
        self.assertIn("chmod 640 /etc/vigil/agent.yml", script)
        self.assertNotIn("chown vigil-agent /etc/vigil/agent.yml", script)
        self.assertIsNone(re.search(r"AGENT_MODE=\"\$\(sed", script), "mode must come from CFG_MODE")

    def test_a_reinstall_restarts_a_running_agent(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": KEY})
        self.assertIn('if [ -n "${VIGIL_TOKEN:-}" ] || systemctl is-active --quiet vigil-agent; then', script)
