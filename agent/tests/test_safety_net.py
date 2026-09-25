"""The agent test package refuses to launch real system-changing commands."""
import subprocess
import unittest


class SafetyNetTests(unittest.TestCase):
    def test_dangerous_commands_are_refused(self):
        for argv in (["systemctl", "restart", "nginx"], ["/sbin/shutdown", "-r", "now"],
                     ["docker", "rm", "-f", "x"], "apt-get install -y curl"):
            with self.assertRaises(RuntimeError, msg=str(argv)):
                subprocess.run(argv, capture_output=True, shell=isinstance(argv, str) and False)

    def test_harmless_commands_still_run(self):
        out = subprocess.run(["printf", "ok"], capture_output=True, text=True)
        self.assertEqual(out.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
