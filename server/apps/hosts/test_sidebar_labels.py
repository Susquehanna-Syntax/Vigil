"""The sidebar can show page names (14.3): the toggle, its saved state applied
before first paint, and labels taken from each icon's own data-tip."""
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class SidebarLabelTests(SimpleTestCase):
    def test_toggle_state_and_labels(self):
        base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
        utils = (ROOT / "static/js/vigil-utils.js").read_text(encoding="utf-8")
        css = (ROOT / "static/css/vigil.css").read_text(encoding="utf-8")
        self.assertIn('data-act="toggleSidebar"', base)
        # Applied in the head script, before the layout first paints.
        head = base[:base.index("</head>")]
        self.assertIn("localStorage.getItem('vigil-sidebar') === 'expanded'", head)
        self.assertIn("function toggleSidebar()", utils)
        self.assertIn("icon.dataset.tip", utils)
        self.assertIn(':root[data-sidebar="expanded"] .sidebar-label', css)
        # Every page icon has a tip to become its label.
        for chunk in base.split('class="sidebar-icon"')[1:]:
            tag = chunk.split(">", 1)[0]
            if "data-page=" in tag:
                self.assertIn("data-tip=", tag, tag)
