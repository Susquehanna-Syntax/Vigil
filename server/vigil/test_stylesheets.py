"""The stylesheet wiring the Stylelint rules depend on (sprint M6).

Colours live in tokens.css and every page loads it through the
_stylesheets.html include, ahead of the numbered parts of the design; the
side-stripe ban is enforced fully by CI's Stylelint step, so these are the
cheap second guard that holds even without a node toolchain."""
import re
from pathlib import Path

from django.test import SimpleTestCase

from vigil.css_text import CSS_DIR, INCLUDE, design_css, stylesheet_paths

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)


def _stylesheet_links(text: str) -> list[tuple[int, str]]:
    """(offset, href) of every stylesheet <link>, in document order.

    The href attribute carries a {% static %} tag whose quoting nests inside
    the attribute's own quotes, so read the static path rather than the raw
    attribute."""
    found = []
    for match in _LINK.finditer(text):
        tag = match.group(0)
        if "stylesheet" not in tag:
            continue
        href = re.search(r"""\{% static ['"]([^'"]+)['"] %\}""", tag)
        if href is None:
            href = re.search(r"""href=['"]([^'"]+)['"]""", tag)
        found.append((match.start(), href.group(1) if href else ""))
    return found


class TokensCssWiringTests(SimpleTestCase):
    def test_every_page_template_loads_the_stylesheets_through_the_include(self):
        for template in sorted(TEMPLATES.rglob("*.html")):
            if template.name == INCLUDE.name:
                continue
            text = template.read_text(encoding="utf-8")
            if "<head" not in text:
                continue
            hrefs = [href for _, href in _stylesheet_links(text)]
            design = [h for h in hrefs
                      if "css/vigil/" in h or "css/tokens.css" in h]
            if not design:
                continue
            self.assertIn(f'{{% include "{INCLUDE.name}" %}}', text,
                          f"{template.relative_to(ROOT)} loads the stylesheets "
                          "directly instead of through the include")
            self.assertFalse(design,
                             f"{template.relative_to(ROOT)} must not link "
                             f"{design[0]} directly")

    def test_the_pages_that_need_the_design_all_include_it(self):
        for name in ("base.html", "login.html", "login_verify.html", "setup.html"):
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn(f'{{% include "{INCLUDE.name}" %}}', text,
                          f"{name} does not load the stylesheets at all")

    def test_the_design_no_longer_holds_the_token_blocks(self):
        vigil = design_css()
        self.assertNotIn(":root {", vigil)
        self.assertNotIn("--bg:", vigil)

    def test_tokens_css_defines_the_ground_in_all_four_themes(self):
        tokens = (CSS_DIR / "tokens.css").read_text(encoding="utf-8")
        for selector in (":root", ':root[data-theme="light"]',
                         ':root[data-theme="ink"]', ':root[data-theme="river"]'):
            start = tokens.index(selector + " {")
            block = tokens[start:tokens.index("}", start)]
            self.assertIn("--bg:", block, f"{selector} lost its ground colour")


class StylesheetPartsTests(SimpleTestCase):
    def test_the_include_links_tokens_css_before_every_design_part(self):
        links = [href for _, href in _stylesheet_links(INCLUDE.read_text(encoding="utf-8"))]
        tokens = [i for i, href in enumerate(links) if "css/tokens.css" in href]
        parts = [i for i, href in enumerate(links) if "css/vigil/" in href]
        self.assertTrue(tokens, "the include does not link tokens.css")
        self.assertTrue(parts, "the include does not link any part of the design")
        self.assertLess(max(tokens), min(parts),
                        "tokens.css must be linked before every part of the design")

    def test_the_include_links_every_part_once_and_nothing_missing(self):
        links = [href for _, href in _stylesheet_links(INCLUDE.read_text(encoding="utf-8"))]
        hrefs = [h for h in links if "css/vigil/" in h]
        self.assertEqual(len(hrefs), len(set(hrefs)),
                         "the include links a part of the design more than once")
        on_disk = sorted(p.name for p in (CSS_DIR / "vigil").glob("*.css"))
        linked = [h.rsplit("/", 1)[-1] for h in hrefs]
        self.assertEqual(sorted(linked), on_disk,
                         "the include and static/css/vigil/ do not hold the same parts")
        for path in stylesheet_paths():
            self.assertTrue(path.is_file(), f"{path} is linked but does not exist")

    def test_the_parts_are_linked_in_their_numbered_order(self):
        names = [p.name for p in stylesheet_paths()]
        numbers = []
        for name in names:
            self.assertRegex(name, r"^\d\d-", f"{name} is not numbered NN-...")
            numbers.append(int(name[:2]))
        self.assertEqual(len(numbers), len(set(numbers)), "two parts share a number")
        self.assertEqual(names, [n for _, n in sorted(zip(numbers, names))],
                         "the include does not link the parts in numbered order")

    def test_the_single_stylesheet_is_gone(self):
        # The old filename is spelled from parts so this file's own grep for it
        # cannot be what "proves" the file is unmentioned.
        old = CSS_DIR / ("vigil" + "." + "css")
        self.assertFalse(old.exists(),
                         "the old single stylesheet still exists; a page could "
                         "quietly load it instead of the parts")


class SideStripeGuardTests(SimpleTestCase):
    def test_no_side_stripes_anywhere_outside_vendor(self):
        for css in sorted(CSS_DIR.rglob("*.css")):
            if "vendor" in css.relative_to(CSS_DIR).parts:
                continue
            text = css.read_text(encoding="utf-8")
            for banned in ("inset 2px 0 0", "border-left: 3px"):
                self.assertNotIn(banned, text, f"{css.name} draws a one-sided stripe ({banned})")


class RenderedIncludeTests(SimpleTestCase):
    """The include's own comment once printed on every page: Django's {# #} is one
    line only, and a longer one is output as text."""

    def test_the_include_renders_links_and_nothing_else(self):
        from django.template.loader import render_to_string

        html = render_to_string(INCLUDE.name)
        self.assertNotIn("{#", html)
        self.assertNotIn("#}", html)
        self.assertNotIn("cascade order", html)
        text = re.sub(r"<link\b[^>]*>", "", html)
        self.assertEqual(text.strip(), "", f"the include outputs text besides its links: {text.strip()!r}")
