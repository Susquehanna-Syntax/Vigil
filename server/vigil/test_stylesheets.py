"""The stylesheet wiring the Stylelint rules depend on (sprint M6).

Colours live in tokens.css and every page loads it before vigil.css; the
side-stripe ban is enforced fully by CI's Stylelint step, so these are the
cheap second guard that holds even without a node toolchain."""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
CSS_DIR = ROOT / "static" / "css"

_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)


def _stylesheet_links(text: str) -> list[tuple[int, str]]:
    """(offset, href) of every stylesheet <link>, in document order."""
    found = []
    for match in _LINK.finditer(text):
        tag = match.group(0)
        if "stylesheet" not in tag:
            continue
        href = re.search(r"href=[\"']([^\"']+)[\"']", tag)
        found.append((match.start(), href.group(1) if href else ""))
    return found


class TokensCssWiringTests(SimpleTestCase):
    def test_every_template_linking_vigil_css_links_tokens_css_first(self):
        for template in sorted(TEMPLATES.rglob("*.html")):
            text = template.read_text(encoding="utf-8")
            links = _stylesheet_links(text)
            vigil = [i for i, (_, href) in enumerate(links) if "css/vigil.css" in href]
            if not vigil:
                continue
            tokens = [i for i, (_, href) in enumerate(links) if "css/tokens.css" in href]
            self.assertTrue(tokens, f"{template} links vigil.css but not tokens.css")
            self.assertLess(max(tokens), min(vigil),
                            f"{template.relative_to(ROOT)} must link tokens.css "
                            "on a line before vigil.css")

    def test_vigil_css_no_longer_holds_the_token_blocks(self):
        vigil = (CSS_DIR / "vigil.css").read_text(encoding="utf-8")
        self.assertNotIn(":root {", vigil)
        self.assertNotIn("--bg:", vigil)

    def test_tokens_css_defines_the_ground_in_all_four_themes(self):
        tokens = (CSS_DIR / "tokens.css").read_text(encoding="utf-8")
        for selector in (":root", ':root[data-theme="light"]',
                         ':root[data-theme="ink"]', ':root[data-theme="river"]'):
            start = tokens.index(selector + " {")
            block = tokens[start:tokens.index("}", start)]
            self.assertIn("--bg:", block, f"{selector} lost its ground colour")


class SideStripeGuardTests(SimpleTestCase):
    def test_no_side_stripes_anywhere_outside_vendor(self):
        for css in sorted(CSS_DIR.rglob("*.css")):
            if "vendor" in css.relative_to(CSS_DIR).parts:
                continue
            text = css.read_text(encoding="utf-8")
            for banned in ("inset 2px 0 0", "border-left: 3px"):
                self.assertNotIn(banned, text, f"{css.name} draws a one-sided stripe ({banned})")
