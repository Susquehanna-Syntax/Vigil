"""The wiki fits a phone: no sideways scrolling at 420 px.

The layout is a CSS grid whose items default to ``min-width: auto``, so a
single long code line used to stretch the whole page to ~960 px (M7 phase 09,
measured in Chromium). These source checks keep the rules that fixed it; the
architect's Playwright measurement is the behavioural check.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def _style() -> str:
    html = (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")
    return html[html.index("<style"):html.index("</style>")]


class WikiMobileTests(SimpleTestCase):
    def test_main_can_shrink_below_its_content(self):
        rule = re.search(r"\n\s*\.main \{[^}]*\}", _style()).group(0)
        self.assertIn("min-width: 0", rule)

    def test_flex_children_can_shrink(self):
        self.assertRegex(_style(), r"\.step-content, \.callout > div \{ min-width: 0; \}")

    def test_wide_blocks_scroll_inside_themselves(self):
        style = _style()
        self.assertRegex(style, r"\.code-block \{[^}]*overflow-x: auto")
        self.assertRegex(style, r"\.table-wrap \{[^}]*overflow-x: auto")
