"""The agent test package refuses to launch real system-changing commands."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import subprocess
import unittest
from pathlib import Path


class SafetyNetTests(unittest.TestCase):
    def test_dangerous_commands_are_refused(self):
        for argv in (["systemctl", "restart", "nginx"], ["/sbin/shutdown", "-r", "now"],
                     ["docker", "rm", "-f", "x"], "apt-get install -y curl"):
            with self.assertRaises(RuntimeError, msg=str(argv)):
                subprocess.run(argv, capture_output=True, shell=isinstance(argv, str) and False)

    def test_harmless_commands_still_run(self):
        out = subprocess.run(["printf", "ok"], capture_output=True, text=True)
        self.assertEqual(out.stdout, "ok")

    def test_every_test_module_installs_the_guard_itself(self):
        # A test module imported on its own (a one-off script doing
        # `from test_x import ...`) skips tests/__init__.py; the per-module
        # import is what keeps the guard on in that case.
        missing = [
            f.name for f in sorted(Path(__file__).parent.glob("test_*.py"))
            if "import tests._safety_net" not in f.read_text()
        ]
        self.assertEqual(missing, [], "add `import tests._safety_net` to these")


if __name__ == "__main__":
    unittest.main()
