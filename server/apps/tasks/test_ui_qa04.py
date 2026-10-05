import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
FLOW_JS = ROOT / "static/js/vigil-playbook-flow.js"
CSS = ROOT / "static/css/vigil.css"


class FlowPanSelectionTests(SimpleTestCase):
    def test_flow_pan_does_not_select_text(self):
        js = FLOW_JS.read_text()
        css = CSS.read_text()

        pointerdown = js[js.index("pointerdown"):js.index("pointermove")]
        self.assertIn("e.preventDefault()", pointerdown)
        self.assertIn("removeAllRanges()", pointerdown)
        self.assertIn("classList.add('fcv-dragging')", pointerdown)

        self.assertIn("classList.remove('fcv-dragging')", js)

        fcv = re.search(r"\.fcv\s*\{([^}]*)\}", css)
        self.assertIsNotNone(fcv)
        self.assertIn("user-select: none", fcv.group(1))
        self.assertIn("body.fcv-dragging", css)


class FixGroupHoverTests(SimpleTestCase):
    def test_fix_hover_only_tints_the_line(self):
        css = CSS.read_text()

        self.assertIn(".vfix-row:hover > .vfix-line", css)
        self.assertIsNone(re.search(r"\.vfix-row:hover\s*\{", css))

        line = re.search(r"\.vfix-line\s*\{([^}]*)\}", css)
        self.assertIsNotNone(line)
        self.assertIn("border-radius", line.group(1))
