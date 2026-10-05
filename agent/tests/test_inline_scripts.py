"""Inline script bodies are arbitrary code, so a managed host runs one only if
its owner approved that exact body by hash.

execute_script used to take only a script_name: a file the host owner had
placed in scripts_dir. An inline body arrives from the server instead, so on a
managed host the agent refuses it unless its sha256 is listed in
allowed_script_hashes. Any edit changes the hash and needs a fresh approval;
full_control hosts run any body, exactly as they already run run_command.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, scripthash
from vigil_agent.config import AgentConfig, load_config


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t",
                mode="full_control")
    base.update(kw)
    return AgentConfig(**base)


def _managed(*hashes):
    return _config(mode="managed", allowlist={"execute_script"},
                   allowed_script_hashes=set(hashes))


def _run_body(body, cfg, shell="sh", inputs=None):
    return executor.execute_action(
        "execute_script", {"shell": shell, "script": body}, cfg, inputs=inputs)


class HashTests(unittest.TestCase):
    def test_hash_normalises_line_endings(self):
        a = scripthash.script_hash("echo hi\r\n")
        self.assertEqual(a, scripthash.script_hash("echo hi"))
        self.assertEqual(a, scripthash.script_hash("echo hi\n\n"))
        self.assertTrue(a.startswith("sha256:"))


@unittest.skipUnless(os.name == "posix", "runs a real sh script")
class GateTests(unittest.TestCase):
    def test_full_control_runs_any_body(self):
        self.assertTrue(_run_body("echo ok", _config()).endswith("ok"))

    def test_managed_refuses_an_unlisted_body(self):
        digest = scripthash.script_hash("echo ok")
        with self.assertRaises(ValueError) as ctx:
            _run_body("echo ok", _managed())
        self.assertTrue("script hash not allowlisted" in str(ctx.exception))
        self.assertTrue(digest in str(ctx.exception))

    def test_managed_runs_a_listed_body(self):
        cfg = _managed(scripthash.script_hash("echo ok"))
        self.assertTrue(_run_body("echo ok", cfg).endswith("ok"))

    def test_editing_the_body_needs_a_new_hash(self):
        cfg = _managed(scripthash.script_hash("echo ok"))
        with self.assertRaises(ValueError):
            _run_body("echo ok2", cfg)

    def test_monitor_never_runs(self):
        with self.assertRaises(ValueError):
            _run_body("echo ok", _config(mode="monitor"))

    def test_inputs_reach_the_inline_body(self):
        out = _run_body("printf '%s' \"$VIGIL_INPUT_APP\"", _config(),
                        inputs={"app": "nginx"})
        self.assertTrue(out.endswith("nginx"))

    def test_output_names_the_body_that_ran(self):
        out = _run_body("echo ok", _config())
        self.assertEqual(out.splitlines()[0], f"[{scripthash.script_hash('echo ok')}]")

    def test_temp_dir_is_removed(self):
        tmp = tempfile.mkdtemp(prefix="vigil-test-")
        with patch.object(executor.tempfile, "mkdtemp", return_value=tmp):
            _run_body("echo ok", _config())
        self.assertFalse(os.path.exists(tmp))


class ParamTests(unittest.TestCase):
    def test_script_and_script_name_conflict(self):
        with self.assertRaises(ValueError):
            executor.execute_action(
                "execute_script",
                {"shell": "sh", "script": "echo", "script_name": "x.sh"}, _config())

    def test_unknown_shell_is_refused(self):
        with self.assertRaises(ValueError):
            _run_body("print(1)", _config(), shell="python")

    def test_powershell_argv(self):
        seen = {}

        def fake_run(cmd, timeout=None, extra_env=None):
            seen["cmd"] = cmd
            return "ok"

        with patch.object(executor, "_run", fake_run):
            _run_body("Write-Output ok", _config(), shell="pwsh")
        self.assertEqual(seen["cmd"][:-1], ["pwsh", "-NoProfile", "-NonInteractive",
                                            "-ExecutionPolicy", "Bypass", "-File"])
        self.assertTrue(seen["cmd"][-1].endswith("script.ps1"))


class ConfigTests(unittest.TestCase):
    def test_bad_hash_entries_are_dropped(self):
        good = scripthash.script_hash("echo ok")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yml"
            path.write_text(
                "server_url: https://vigil.example.com\n"
                f"agent_token: {'a' * 32}\n"
                "mode: managed\n"
                "allowed_script_hashes:\n"
                f"  - {good.upper().replace('SHA256:', 'sha256:')}\n"
                "  - sha256:nothex\n")
            os.chmod(path, 0o600)
            cfg = load_config(path)
        self.assertEqual(cfg.allowed_script_hashes, {good})


if __name__ == "__main__":
    unittest.main()
