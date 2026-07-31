"""The core/Business boundary, enforced rather than documented."""
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

SERVER_DIR = Path(__file__).resolve().parent.parent


class AgplOnlyBuildTests(SimpleTestCase):
    """Removing an app from INSTALLED_APPS with override_settings does NOT
    unregister its reverse relations, so this has to be a real subprocess."""

    def test_core_site_paths_survive_without_the_business_app(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "vigil.settings_agpl_only",
            "USE_SQLITE": "1",
            "DJANGO_SECRET_KEY": "agpl-probe",
            "VIGIL_SIGNING_KEY_SEED": "sL/+T7Bu4Kq/6eJgvFyEIb+vnqxmDGHK5UHj0yekqC8=",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "vigil._agpl_probe"],
            cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"core reached a Business relation without the façade:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        self.assertIn("agpl-only probe OK", proc.stdout)
