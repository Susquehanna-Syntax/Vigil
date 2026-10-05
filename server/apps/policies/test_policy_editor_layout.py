"""The policy editor's layout, pinned by source scan (no JS runner)."""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class PolicyEditorLayoutTests(SimpleTestCase):
    def setUp(self):
        self.html = (ROOT / "templates/pages/_policies.html").read_text(encoding="utf-8")
        self.js = (ROOT / "static/js/vigil-policies.js").read_text(encoding="utf-8")
        self.css = (ROOT / "static/css/vigil.css").read_text(encoding="utf-8")

    def test_labels_are_short_and_hints_sit_below(self):
        self.assertIsNone(re.search(r'<label[^>]*>[^<]*<span class="pol-opt"', self.html))
        self.assertGreaterEqual(self.html.count('class="pol-hint"'), 6)
        for label in (">Defer updates (days)</label>", ">Restart</label>", ">Linux updates</label>"):
            self.assertIn(label, self.html)

    def test_modal_is_wide_enough(self):
        self.assertIn('class="modal modal-wide pol-modal"', self.html)
        rule = re.search(r"\.modal\.pol-modal\s*\{[^}]*\}", self.css)
        self.assertIsNotNone(rule, "no .modal.pol-modal rule in vigil.css")
        self.assertIn("760px", rule.group(0))

    def test_save_closes_the_editor(self):
        start = self.js.index("async function savePolicy(")
        body = self.js[start:self.js.index("async function deletePolicy(", start)]
        self.assertIn("closePolicyEditor();", body)
        self.assertNotIn("setPolicyTab('preview')", body)

    def test_page_explains_policies(self):
        self.assertIn('id="pol-explain"', self.html)
        self.assertIn("How policies work", self.html)
        for step in ("<li><strong>Pick hosts.</strong>",
                     "<li><strong>Say what they should have.</strong>",
                     "<li><strong>Vigil compares.</strong>",
                     "<li><strong>It fixes drift in the window.</strong>"):
            self.assertIn(step, self.html)
        self.assertIn("'pol-explain'", self.js)
