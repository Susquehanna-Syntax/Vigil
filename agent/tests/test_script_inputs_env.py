"""Inputs pasted into a command line are code; in an environment variable they are data.

``execute_script`` exposes every resolved task input as
``VIGIL_INPUT_<NAME>`` — ``$VIGIL_INPUT_APP`` in bash, ``$env:VIGIL_INPUT_APP``
in PowerShell.  ``nginx; touch pwned`` arrives as the literal text of a
variable, never as a second command.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor
from vigil_agent.config import AgentConfig
from vigil_agent.runtime import TaskRuntime


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control"}
    base.update(kw)
    return AgentConfig(**base)


@unittest.skipUnless(os.name == "posix", "shell script")
class ScriptInputEnvTests(unittest.TestCase):
    def _run_script(self, variables):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "show.sh"
            script.write_text('#!/bin/sh\nprintf \'%s\' "$VIGIL_INPUT_APP"\n')
            script.chmod(0o755)
            cfg = _config(scripts_dir=Path(tmp))
            task = TaskRuntime(
                {"steps": [{"name": "s", "action": "execute_script",
                            "params": {"script_name": "show.sh"}}],
                 "variables": variables},
                cfg,
            )
            return task.run()[0].output

    def test_script_sees_input_as_env(self):
        self.assertEqual(self._run_script({"app": "nginx"}), "nginx")

    def test_injection_attempt_stays_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            pwned = Path(tmp) / "pwned"
            output = self._run_script({"app": f"nginx; touch {pwned}"})
            self.assertEqual(output, f"nginx; touch {pwned}")
            self.assertFalse(pwned.exists())


class InputEnvFormatTests(unittest.TestCase):
    def test_number_and_boolean_formatting(self):
        self.assertEqual(
            executor._input_env({"n": 3.0, "f": 2.5, "b": True}),
            {"VIGIL_INPUT_N": "3", "VIGIL_INPUT_F": "2.5",
             "VIGIL_INPUT_B": "true"})

    def test_none_and_empty_produce_no_env(self):
        self.assertEqual(executor._input_env(None), {})
        self.assertEqual(executor._input_env({}), {})

    def test_bad_names_are_skipped(self):
        self.assertEqual(
            executor._input_env({"ok": "1", "bad-name": "2"}),
            {"VIGIL_INPUT_OK": "1"})

    def test_nul_byte_is_refused(self):
        with self.assertRaises(ValueError):
            executor._input_env({"x": "a\x00b"})

    def test_other_actions_get_no_input_env(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return "ok"

        with patch.object(executor, "_run", fake_run):
            executor.execute_action("run_command", {"command": "true"},
                                    _config(), inputs={"app": "x"})
        self.assertEqual(seen.get("extra_env"), None)


if __name__ == "__main__":
    unittest.main()
