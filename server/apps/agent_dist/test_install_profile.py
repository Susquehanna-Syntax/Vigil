"""install.sh redeems a one-time VIGIL_CONFIG_TOKEN for an agent profile's
agent.yml (2026.15). The block is run for real, in bash, with curl replaced by
a function that serves the profile body, against a scratch agent.yml."""
import os
import subprocess
import tempfile
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

START = "# VIGIL_TOKEN ends up inside sed replacements"
END = "# ── Service installation"
CONFIG_TOKEN = "cfg_" + "A" * 40

PROFILE = (
    "# Vigil agent config — written by the installer from an enroll profile. This file is now the authority.\n"
    "server_url: https://relay-1.example.com\n"
    'agent_token: "REPLACE_WITH_TOKEN"\n'
    "fallback_urls:\n- https://relay-2.example.com\n- https://vigil.test\n"
    "mode: managed\n"
    "allowlist:\n- check_service\n- restart_service\n"
    "allow_remote_shell: true\n"
    "tags:\n- web\n"
)

CURL = r'''
curl() {
  local out="" url=""
  while [ $# -gt 0 ]; do
    case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac
    shift
  done
  echo "$url" >> "$CURL_LOG"
  [ -n "$PROFILE_FAIL" ] && return 22
  [ -n "$out" ] && printf '%s' "$PROFILE_BODY" > "$out"
  return 0
}
'''


class InstallShProfileTests(SimpleTestCase):
    def setUp(self):
        script = render_to_string("agent_install.sh", {"base_url": "https://vigil.test", "public_key": "K="})
        self.block = script[script.index(START):script.index(END)]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "agent.yml"
        self.log = Path(self.tmp.name) / "curl.log"

    def _run(self, existing=None, env=None, body=PROFILE):
        if existing is not None:
            self.cfg.write_text(existing)
            self.cfg.chmod(0o600)
        block = (self.block.replace("/etc/vigil/agent.yml", str(self.cfg))
                 .replace("/etc/vigil", str(self.cfg.parent)))
        harness = ('VIGIL_SERVER="https://vigil.test"\nchown() { :; }\n' + CURL + block)
        result = subprocess.run(
            ["bash", "-c", harness], capture_output=True, text=True, timeout=30,
            env={**os.environ, "CURL_LOG": str(self.log), "PROFILE_BODY": body,
                 "VIGIL_CONFIG_TOKEN": CONFIG_TOKEN, **(env or {})})
        return result, (self.cfg.read_text() if self.cfg.exists() else "")

    def test_fresh_install_takes_the_profile_and_the_wizard_token(self):
        result, text = self._run(env={"VIGIL_TOKEN": "tok_0123456789abcdef"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"https://vigil.test/agent/enroll-config/{CONFIG_TOKEN}/", self.log.read_text())
        self.assertIn('agent_token: "tok_0123456789abcdef"', text)
        self.assertNotIn("REPLACE_WITH_TOKEN", text)
        self.assertIn("server_url: https://relay-1.example.com", text)
        self.assertRegex(text, r"(?m)^mode: managed$")
        self.assertIn("- restart_service", text)
        self.assertIn("allow_remote_shell: true", text)
        self.assertIn('server_public_key: "K="', text)
        self.assertIn("Profile applied: mode managed, checks in with https://relay-1.example.com, "
                      "2 fallback address(es).", result.stdout)

    def test_without_a_wizard_token_one_is_generated(self):
        result, text = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("REPLACE_WITH_TOKEN", text)
        self.assertRegex(text, r'(?m)^agent_token: "[0-9a-f]{64}"$')

    def test_an_existing_config_is_replaced_but_its_token_kept(self):
        if os.geteuid() == 0:
            self.skipTest("the scratch file would be root-owned")
        result, text = self._run('server_url: "https://old.example"\nagent_token: "kept_0123456789abcdef"\n'
                                 'mode: full_control\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('agent_token: "kept_0123456789abcdef"', text)
        self.assertNotIn("old.example", text)
        self.assertRegex(text, r"(?m)^mode: managed$")

    def test_vigil_mode_still_overrides_the_profile(self):
        result, text = self._run(env={"VIGIL_MODE": "monitor"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(text, r"(?m)^mode: monitor$")

    def test_a_rejected_token_stops_the_install(self):
        result, _ = self._run(env={"PROFILE_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejected the config token", result.stderr)

    def test_a_body_that_is_not_a_profile_is_refused(self):
        result, _ = self._run(body="<html>login</html>\n")
        self.assertNotEqual(result.returncode, 0)
        result, _ = self._run(body="# Vigil agent config\nagent_token: real-token-here\n")
        self.assertNotEqual(result.returncode, 0)

    def test_a_malformed_config_token_never_reaches_curl(self):
        result, _ = self._run(env={"VIGIL_CONFIG_TOKEN": "a/../../x"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
