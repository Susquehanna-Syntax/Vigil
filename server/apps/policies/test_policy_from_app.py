"""Apps page → policy (14.3): a Policy… button per app that opens the policy
editor with that app's rules added, one per (package id, source)."""
import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.tasks.spec import _APP_SOURCE_CHOICES

ROOT = Path(__file__).resolve().parents[2]


class PolicyFromAppTests(SimpleTestCase):
    def setUp(self):
        self.apps = (ROOT / "static/js/vigil-apps.js").read_text(encoding="utf-8")
        self.pol = (ROOT / "static/js/vigil-policies.js").read_text(encoding="utf-8")

    def test_the_button_and_its_handler(self):
        self.assertIn('data-app-policy="${escAttr(key)}"', self.apps)
        self.assertIn("delegateClick('[data-app-policy]'", self.apps)
        self.assertIn("if (ev.target.closest('a, button')) return;", self.apps,
                      "clicking the button must not also expand the row")
        self.assertIn("async function policyFromApp(appName, pairs)", self.pol)
        self.assertIn("state: 'latest'", self.pol)

    def test_only_sources_a_rule_can_name(self):
        listed = re.search(r"const POLICY_APP_SOURCES = \[([^\]]*)\]", self.apps).group(1)
        self.assertEqual(set(re.findall(r"'(\w+)'", listed)), set(_APP_SOURCE_CHOICES))
        self.assertIn("if (!r.managed || !POLICY_APP_SOURCES.includes(r.source)) continue;", self.apps)
