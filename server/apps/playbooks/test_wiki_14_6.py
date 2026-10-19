"""QA-43: 2026.14.6 must be written up in the wiki.

Placement is checked because a subsection that landed in the wrong section
reads as a promise the feature never made, and the go to facts are checked
verbatim because they are the semantics a reader writes YAML against. The
generated action reference must survive untouched: prose edits around it are
fine, edits inside it are hand-edits the generator would wipe.
"""
import re
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[3]
WIKI = REPO_ROOT / "wiki" / "vigil-wiki.html"

NEW_IDS = ("condition-rows", "goto", "alert-actions", "reports", "report-charts")


def _wiki() -> str:
    return WIKI.read_text(encoding="utf-8")


def _section_of(text: str, pos: int) -> str:
    start = text.rfind('<section class="section" id="', 0, pos)
    return text[start + len('<section class="section" id="'):].split('"', 1)[0]


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


class Wiki146Tests(SimpleTestCase):
    def test_new_headings_exist(self):
        text = _wiki()
        for anchor_id in NEW_IDS:
            self.assertEqual(
                text.count(f'id="{anchor_id}"'), 1,
                f"{anchor_id} must appear exactly once",
            )

    def test_placement(self):
        text = _wiki()
        quoting = text.index("<h3>Quoting step ids</h3>")
        flow_editor = text.index("<h3>The flow editor</h3>")
        for anchor_id in ("condition-rows", "goto"):
            pos = text.index(f'<h3 id="{anchor_id}"')
            self.assertGreater(pos, flow_editor, f"{anchor_id} precedes the flow editor")
            self.assertLess(pos, quoting, f"{anchor_id} comes after quoting step ids")
        self.assertEqual(_section_of(text, text.index('<h3 id="condition-rows"')),
                         "relevance-and-branches")
        self.assertEqual(_section_of(text, text.index('<h3 id="goto"')),
                         "relevance-and-branches")

        pos = text.index('<h3 id="alert-actions"')
        self.assertLess(pos, text.index("<h3>Default Thresholds</h3>"))
        self.assertEqual(_section_of(text, pos), "alerts")

        pos = text.index('<h3 id="reports"')
        self.assertLess(pos, text.index("<h3>Operator actions</h3>"))
        self.assertEqual(_section_of(text, pos), "deployments")
        self.assertEqual(_section_of(text, text.index('<h3 id="report-charts"')),
                         "deployments")

    def test_goto_facts(self):
        text = _wiki()
        start = text.index('<h3 id="goto"')
        ends = [e for e in (text.find(nxt, start + 5) for nxt in
                            ("<h3", "<h2", "</section>")) if e != -1]
        subsection = text[start:min(ends)] if ends else text[start:]
        for fact in ("max:", "jumped over by go to", "last item in its lane", "8",
                     "retry-the-service.yaml"):
            self.assertIn(fact, subsection, f"go to subsection lacks {fact!r}")

    def test_report_charts_listed(self):
        text = _wiki()
        start = text.index('id="report-charts"')
        end = text.index("<h3", start)
        charts = text[start:end]
        for title in ("Success rate", "Runs per day", "Failures by step",
                      "Branch paths", "Duration", "Host outcomes",
                      "Automation fires"):
            self.assertIn(title, charts, f"charts table lacks {title}")

    def test_resize_paragraph(self):
        text = _wiki()
        self.assertEqual(text.count("double-click the grip"), 1)
        self.assertGreater(text.index("double-click the grip"),
                           text.index("<h3>The flow editor</h3>"))

    def test_no_inline_style_or_handlers_added(self):
        text = _wiki()
        slices = (
            _slice(text, '<h3 id="condition-rows"', "<h3>Quoting step ids</h3>"),
            _slice(text, '<h3 id="alert-actions"', "<h3>Default Thresholds</h3>"),
            _slice(text, '<h3 id="reports"', "<h3>Operator actions</h3>"),
        )
        for chunk in slices:
            self.assertNotIn(' style="', chunk, "inline style added")
            self.assertIsNone(re.search(r" on[A-Za-z]+=\"", chunk),
                              "inline event handler added")
            stray = {c for c in chunk if ord(c) > 0x2FFF} - {"→", "×"}
            self.assertFalse(stray, f"non-text character added: {stray}")

    def test_generated_block_untouched(self):
        try:
            head = subprocess.run(
                ["git", "show", "HEAD:wiki/vigil-wiki.html"],
                capture_output=True, text=True, cwd=REPO_ROOT, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        begin = "<!-- BEGIN GENERATED"
        end = "<!-- END GENERATED"
        self.assertEqual(_slice(_wiki(), begin, end), _slice(head, begin, end),
                         "the generated action reference was hand-edited")
