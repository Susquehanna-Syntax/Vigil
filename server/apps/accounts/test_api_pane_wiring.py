"""Settings → API & Webhooks is three files nothing compiles together.

The pane's HTML host, the script that fills it, and the nav handler that opens
it are joined by nothing but a string. A rename anywhere in that chain renders
a card that says "Loading…" forever with every gate in the repo still green —
which is exactly how the Jackil keys card nearly shipped, and why WiringTests
exists there. This is the same guard for the scoped-key and webhook cards.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
JS = REPO / "server" / "static" / "js"
TEMPLATES = REPO / "server" / "templates"


def _read(path: Path) -> str:
    return path.read_text()


class ApiPaneWiringTests(SimpleTestCase):

    def test_pane_and_nav_exist(self):
        settings_html = _read(TEMPLATES / "pages" / "_settings.html")
        self.assertEqual(settings_html.count('data-pane="api"'), 2,
                         "the nav button and the pane both name the pane")
        self.assertEqual(settings_html.count('id="api-keys"'), 1)
        self.assertEqual(settings_html.count('id="webhooks"'), 1)

    def test_script_is_loaded_and_opened(self):
        base = _read(TEMPLATES / "base.html")
        self.assertIn("js/vigil-api-keys.js", base,
                      "the pane's script is never served, so both cards load forever")

        settings_js = _read(JS / "vigil-settings.js")
        # The guard and the call sit on consecutive lines, so `.` must cross a
        # newline — otherwise a moved call is exactly what goes unnoticed.
        self.assertRegex(settings_js,
                         r"dataset\.pane === 'api'[\s\S]*?loadApiPane\(\)",
                         "opening the pane never asks for its data")

        api_js = _read(JS / "vigil-api-keys.js")
        self.assertIn("async function loadApiPane(", api_js)

    def test_every_data_attribute_is_bound(self):
        api_js = _read(JS / "vigil-api-keys.js")
        for attribute in ("data-ak-create", "data-ak-revoke", "data-wh-create",
                          "data-wh-test", "data-wh-log", "data-wh-toggle",
                          "data-wh-rotate", "data-wh-delete"):
            self.assertIn(attribute, api_js)
            pattern = re.compile(r"delegateClick\(\s*['\"`][^'\"`]*" + attribute)
            self.assertIsNotNone(pattern.search(api_js),
                                 f"{attribute} appears in markup but no delegateClick binds it")

    def test_no_inline_handlers_or_side_borders(self):
        api_js = _read(JS / "vigil-api-keys.js")
        for bad in ('onclick="', 'onchange="', "onclick='", "onchange='"):
            self.assertNotIn(bad, api_js, f"{bad} in vigil-api-keys.js puts "
                                          "'unsafe-inline' back in the CSP")
        self.assertNotIn("border-left", api_js)
        self.assertNotIn("eval(", api_js)
