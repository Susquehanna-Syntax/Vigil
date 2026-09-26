"""The runtime used to rewrite every ``{{…}}`` it found and blank unknown names.

So ``docker ps --format '{{.Names}}'`` ran as ``docker ps --format ''``.
After the ``${{ … }}`` marker, only that marker is a template; bare braces
(Go templates, ``find -exec {} \\;``, PowerShell blocks) reach the handler
exactly as the server signed them.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.config import AgentConfig
from vigil_agent.runtime import TaskRuntime


def _config(**kw):
    base = {
        "server_url": "https://vigil.example.com",
        "agent_token": "t",
        "mode": "full_control",
    }
    base.update(kw)
    return AgentConfig(**base)


class LiteralBracesTests(unittest.TestCase):
    def _run_params(self, command, variables=None):
        """Run a single run_command step and return the params the handler saw."""
        recorded = {}

        def fake_execute_action(action, params, config, **kwargs):
            recorded[action] = params
            return "ok"

        payload = {
            "steps": [{
                "name": "s1",
                "action": "run_command",
                "params": {"command": command},
            }],
            "variables": variables or {},
        }
        with patch("vigil_agent.executor.execute_action",
                   fake_execute_action):
            TaskRuntime(payload, _config()).run()
        return recorded["run_command"]

    def test_go_template_braces_reach_the_handler(self):
        command = "docker ps --format '{{.Names}}'"
        seen = self._run_params(command)
        self.assertEqual(seen["command"], command)

    def test_find_exec_braces_reach_the_handler(self):
        command = r"find /tmp -name '*.tmp' -exec rm {} \;"
        seen = self._run_params(command)
        self.assertEqual(seen["command"], command)

    def test_new_marker_still_resolves(self):
        seen = self._run_params("apt-get install ${{ pkg }}",
                                {"pkg": "nginx"})
        self.assertEqual(seen["command"], "apt-get install nginx")

    def test_inputs_path_resolves(self):
        seen = self._run_params("${{ inputs.pkg }}", {"pkg": "nginx"})
        self.assertEqual(seen["command"], "nginx")

    def test_exact_marker_keeps_its_type(self):
        seen = self._run_params("${{ n }}", {"n": 3})
        self.assertEqual(seen["command"], 3)
        self.assertIsInstance(seen["command"], int)


if __name__ == "__main__":
    unittest.main()
