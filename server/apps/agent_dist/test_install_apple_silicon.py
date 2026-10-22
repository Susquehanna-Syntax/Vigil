"""install.sh on a Mac: Apple Silicon only, decided by the hardware.

The platform block is lifted out of the real template and run under bash with
``uname`` and ``sysctl`` stubbed, so the test sees exactly what a Mac would.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATE = Path(settings.BASE_DIR) / "templates" / "agent_install.sh"


def _platform_block() -> str:
    text = TEMPLATE.read_text()
    match = re.search(r'^OS=\$\(uname -s.*?^esac\n', text, re.M | re.S)
    assert match, "no platform block in agent_install.sh"
    return match.group(0) + 'echo "PLATFORM=$PLATFORM"\n'


class MacPlatformTests(SimpleTestCase):
    def _run(self, machine: str, arm64: str):
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp)
            (stubs / "uname").write_text(
                f'#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else echo {machine}; fi\n')
            (stubs / "sysctl").write_text(f"#!/bin/sh\necho {arm64}\n")
            for stub in stubs.iterdir():
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
            return subprocess.run(["bash", "-c", _platform_block().replace("{{ base_url }}", "x")],
                                  capture_output=True, text=True, env=env, timeout=30)

    def test_an_intel_mac_is_refused_with_a_reason(self):
        result = self._run("x86_64", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Apple Silicon", result.stderr)
        self.assertNotIn("PLATFORM=", result.stdout)

    def test_a_rosetta_terminal_on_apple_silicon_still_gets_arm64(self):
        result = self._run("x86_64", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLATFORM=darwin-arm64", result.stdout)

    def test_apple_silicon_gets_arm64(self):
        result = self._run("arm64", "1")
        self.assertIn("PLATFORM=darwin-arm64", result.stdout)
