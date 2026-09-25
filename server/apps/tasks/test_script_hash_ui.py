"""The editor preview shows an inline script's hash, not its body.

An inline script action carries ``script_sha256`` (phase 06b) and the whole
body under ``params.script``. The preview must show the hash — that is what a
host owner copies into ``vigil-agent allow-script`` — and never dump the body
into the one-line step rendering. The hash row's Copy button must stay a
declarative click handler: inline ``onclick`` attributes keep 'unsafe-inline'
in the CSP, which would let an injected <script> run (see
``apps.hosts.test_inline_handlers``).

Vigil has no JS test runner; these SimpleTestCases read the JS source the way
``test_inline_handlers.py`` does.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

JS = Path(settings.BASE_DIR) / "static" / "js" / "vigil-tasks.js"
DOCS = Path(settings.BASE_DIR).parent / "docs" / "AGENT-PRIVILEGES.md"


class HashRowTests(SimpleTestCase):
    def setUp(self):
        self.src = JS.read_text()

    def test_preview_shows_the_hash_not_the_body(self):
        self.assertIn(
            "script_sha256", self.src, "the preview does not read a.script_sha256"
        )
        self.assertIn(
            "data-copy-hash",
            self.src,
            "the Copy button lost its data-copy-hash attribute",
        )
        self.assertTrue(
            "k === 'script' ? `script=${String(v).replace(/\\n+$/, '').split('\\n').length} lines`"
            in self.src,
            "the script param is no longer rendered as 'script=<N lines>' — the body would leak into the step line",
        )

    def test_copy_button_has_no_inline_handler(self):
        idx = self.src.find("preview-step-hash")
        self.assertNotEqual(idx, -1, "the preview-step-hash row is gone")
        window = self.src[idx : idx + 300]
        self.assertNotIn(
            "onclick", window, "the hash row carries an inline onclick handler"
        )


class DocsTests(SimpleTestCase):
    def test_docs_explain_reapproval_and_commands(self):
        text = DOCS.read_text()
        self.assertEqual(
            text.count("## Inline scripts and hash approval"),
            1,
            "docs/AGENT-PRIVILEGES.md must have exactly one 'Inline scripts and hash approval' section",
        )
        for needle in (
            "allowed_script_hashes",
            "vigil-agent allow-script",
            "sha256sum",
            "Get-FileHash",
            "changes the hash",
        ):
            self.assertIn(needle, text, f"the docs no longer mention {needle!r}")
