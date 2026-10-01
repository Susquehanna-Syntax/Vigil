"""M12: the design language's four themes — Dark, Paper, River and Ink.

Every page was screenshotted in each theme at 1440 and 420 wide; these pin
the token layer and the wiring so a later change cannot quietly undo it.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

SERVER = Path(__file__).resolve().parents[2]
CSS = (SERVER / "static" / "css" / "vigil.css").read_text()
JS = SERVER / "static" / "js"
TEMPLATES = SERVER / "templates"
PASTELS = ("rose", "lavender", "mint", "peach", "sky", "lemon", "coral")


def _block(selector):
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("}", start)]


class ThemeTokenTests(SimpleTestCase):
    def test_each_theme_sets_its_ground(self):
        self.assertIn("--bg: #1c1c21;", _block(":root"))
        self.assertIn("--bg: #ede8bb;", _block(':root[data-theme="light"]'))
        self.assertIn("--bg: #141c2b;", _block(':root[data-theme="river"]'))
        self.assertIn("--bg: #100f0f;", _block(':root[data-theme="ink"]'))

    def test_paper_swaps_in_deeper_inks(self):
        root, paper = _block(":root"), _block(':root[data-theme="light"]')
        for p in PASTELS:
            self.assertIn(f"--{p}-ink: var(--{p});", root)
            self.assertRegex(paper, rf"--{p}-ink: #[0-9a-f]{{6}};")

    def test_chrome_tokens_replace_dark_literals(self):
        for token in ("--on-accent", "--glass", "--glass-edge", "--wash", "--grid-line", "--scrim"):
            self.assertIn(token + ":", _block(":root"))
        self.assertNotIn("rgba(28, 28, 33, 0.78);\n  backdrop-filter", CSS)
        self.assertNotRegex(CSS, r"color: #1c1c21")

    def test_pastel_text_reads_the_ink_token(self):
        # A pastel as text is illegible on Paper; as a fill it is fine.
        offenders = re.findall(r"(?:^|[^-])color: ?var\(--(?:%s)\)" % "|".join(PASTELS), CSS)
        self.assertEqual(offenders, [])

    def test_reduce_motion_is_switchable(self):
        self.assertIn("html.rm-sim *", CSS)


class ComponentStandardTests(SimpleTestCase):
    def test_filled_accent_buttons_use_on_accent_text(self):
        # var(--bg) was cream-on-pastel on Paper.
        for accent in ("mint", "rose", "sky", "lav", "peach", "lemon"):
            rule = CSS[CSS.index(f".btn-{accent} {{"):]
            rule = rule[:rule.index("}")]
            self.assertIn("color: var(--on-accent)", rule, accent)

    def test_pastel_tints_go_through_the_channel_tokens(self):
        literal = r"rgba\((?:242,160,184|186,168,232|126,221,181|240,184,136|130,196,238|226,212,120|240,144,128),"
        self.assertNotRegex(CSS.replace(" ", ""), literal)

    def test_tabs_scroll_instead_of_wrapping_on_phones(self):
        self.assertIn("overflow-x: auto", _block(".tab-bar"))
        self.assertIn("white-space: nowrap", _block(".tab"))

    def test_chart_js_is_never_handed_a_css_variable(self):
        # A canvas cannot resolve var(); the line silently drew in the default.
        src = (JS / "vigil-widgets.js").read_text()
        self.assertNotRegex(src, r"borderColor: 'var\(--")


class ThemeWiringTests(SimpleTestCase):
    def test_first_paint_knows_every_theme(self):
        base = (TEMPLATES / "base.html").read_text()
        self.assertIn("t !== 'light' && t !== 'river' && t !== 'ink'", base)
        self.assertIn("vigil-reduce-motion", base)

    def test_set_theme_accepts_four_and_tells_the_charts(self):
        utils = (JS / "vigil-utils.js").read_text()
        self.assertIn("const THEMES = ['dark', 'light', 'river', 'ink'];", utils)
        self.assertIn("new CustomEvent('vigil:theme'", utils)
        self.assertIn("function tok(name)", utils)
        self.assertIn("document.addEventListener('vigil:theme'", (JS / "vigil-monitor.js").read_text())

    def test_charts_read_tokens_not_dark_greys(self):
        for name in ("vigil-monitor.js", "vigil-host-cards.js", "vigil-widgets.js"):
            src = (JS / name).read_text()
            for literal in ("'#32323a'", "'#232329'", "'#3a3a43'", "'#8b8ba3'"):
                self.assertNotIn(literal, src, f"{literal} in {name}")

    def test_settings_offers_four_cards_system_and_motion(self):
        html = (TEMPLATES / "pages" / "_settings.html").read_text()
        for theme in ("dark", "light", "river", "ink", "system"):
            self.assertIn(f'id="theme-{theme}"', html)
        self.assertIn('id="motion-reduce"', html)
        self.assertNotIn("{# The previews", html)

    def test_the_public_status_page_keeps_its_own_palette(self):
        # It does not load vigil.css, so an -ink token there would be undefined.
        self.assertNotIn("-ink)", (TEMPLATES / "status_public.html").read_text())
