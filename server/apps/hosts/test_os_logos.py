"""Every OS a logo can be requested for has an image file behind it.

These used to be hand-authored inline SVG paths, and four of them were wrong
for a long time with nothing failing: Mint, Debian and Red Hat each drew
their glyph in the colour of their own disc, so they rendered as flat
circles, and Fedora's path drew a capital D rather than an "f". Nothing
errors when an icon is merely wrong.

They are now real logo images (Simple Icons, CC0-1.0, on a brand-coloured
disc; Windows and Bazzite drawn by hand because Simple Icons carries
neither). A wrong path is no longer possible — a *missing* file is, which is
what this checks. The files are bundled rather than hot-linked because Vigil
runs on networks with no route to the internet.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase


def _utils_js() -> str:
    return (Path(settings.BASE_DIR) / "static" / "js"
            / "vigil-utils.js").read_text(encoding="utf-8")


def _table() -> str:
    """The OS_LOGOS literal. The closing bracket is searched for *after* the
    opening one — the file has earlier `];` sequences."""
    source = _utils_js()
    start = source.index("const OS_LOGOS = [")
    return source[start:source.index("];", start)]


def _pairs() -> list[tuple[str, str]]:
    return re.findall(r"\['([^']+)',\s*'([^']+)'\]", _table())


def _slugs() -> list[str]:
    """Every slug named in the OS_LOGOS table, plus the fallback."""
    return [slug for _needle, slug in _pairs()] + ["linux"]


def _logo_dir() -> Path:
    return Path(settings.BASE_DIR) / "static" / "img" / "os"


class OsLogoAssetTests(TestCase):
    def test_the_table_was_parsed(self):
        """Guard the guard: a parser matching nothing would pass silently."""
        slugs = _slugs()
        self.assertGreaterEqual(len(slugs), 15, slugs)
        for expected in ("ubuntu", "debian", "fedora", "linuxmint", "redhat"):
            self.assertIn(expected, slugs)

    def test_every_slug_has_an_image(self):
        missing = [s for s in set(_slugs())
                   if not (_logo_dir() / f"{s}.png").is_file()]
        self.assertEqual(missing, [], f"no image for: {missing}")

    def test_the_fallback_image_exists(self):
        """An unrecognised OS falls back to Tux; that file has to be there or
        every unknown host shows a broken image."""
        self.assertTrue((_logo_dir() / "linux.png").is_file())

    def test_no_image_is_empty_or_a_stub(self):
        """A 404 body saved as a .png is still a file. Check for the PNG magic
        number and a plausible size."""
        for path in sorted(_logo_dir().glob("*.png")):
            data = path.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n",
                             f"{path.name} is not a PNG")
            self.assertGreater(len(data), 500, f"{path.name} is suspiciously small")

    def test_derivatives_are_tested_before_their_parents(self):
        """Order is load-bearing: Linux Mint's os_family is ubuntu and
        Bazzite's is rhel, so a parent listed first would claim both."""
        needles = [needle for needle, _slug in _pairs()]
        self.assertLess(needles.index("mint"), needles.index("ubuntu"))
        self.assertLess(needles.index("bazzite"), needles.index("fedora"))

    def test_nothing_still_hand_draws_a_logo(self):
        """The defect class this replaced: an inline path whose fill matches
        the disc it sits on cannot recur if there are no inline paths."""
        source = _utils_js()
        block = source[source.index("const OS_LOGOS = ["):source.index("function osLogo(")]
        self.assertNotIn("<circle", block)
        self.assertNotIn("<path", block)
