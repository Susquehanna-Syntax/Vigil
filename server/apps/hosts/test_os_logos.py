"""No distro logo may paint its glyph in the colour of its own disc.

Four of them did. Fedora drew an arc closed by a straight line — a capital
D, not an "f". Mint's leaf was ``fill="#87CF3E"`` on a ``#87CF3E`` circle,
Debian's swirl ``#A80030`` on ``#A80030``, and Red Hat's brim and crown
``#EE0000`` on ``#EE0000``: three flat discs with nothing on them at all.

Nothing failed. The icons rendered, the page loaded, and the mistake was
invisible until the logos were shown at 42px on the catalog cards.

Parsed out of the JS source rather than executed, so this needs no Node.
The check is deliberately narrow — a glyph is only flagged when *every*
colour it paints with equals the disc colour, since several logos layer a
disc-coloured shape over a darker one on purpose (Bazzite's hex centre, the
generic Tux's eyes), and those are correct.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

#: Logos whose glyph legitimately repaints in the disc colour, over a shape
#: of a different colour drawn beneath it: Bazzite's hexagon centre and the
#: generic penguin's eyes, both sitting on a dark shape.
_LAYERED = {"bazzite", "generic"}

_ELEMENT = re.compile(r"<(?:path|circle|rect|ellipse|text|polygon)\b[^>]*>")
_FILL = re.compile(r'fill="(#[0-9A-Fa-f]{6})"')
_STROKE = re.compile(r'stroke="(#[0-9A-Fa-f]{6})"')


def _os_logo_source() -> str:
    path = Path(settings.BASE_DIR) / "static" / "js" / "vigil-utils.js"
    text = path.read_text(encoding="utf-8")
    start = text.index("function osLogo")
    return text[start:text.index("function mountModal", start)]


def _branches() -> list[tuple[str, str, str]]:
    """(key, markup, disc colour) for every ``return s(...)`` branch.

    A branch is named for the ``n.includes()`` tests of the ``if`` it belongs
    to — the ones between the previous branch and this return. A return with
    no ``if`` of its own is the final fallback, named "generic".
    """
    source = _os_logo_source()
    out, cursor = [], 0
    for match in re.finditer(
            r"return s\(\s*(?P<body>.+?),\s*'(?P<colour>#[0-9A-Fa-f]{6})'\s*\)",
            source, re.S):
        between = source[cursor:match.start()]
        cursor = match.end()
        keys = re.findall(r"n\.includes\('([^']+)'\)", between)
        # The body may be several single-quoted literals joined with '+'.
        markup = "".join(re.findall(r"'([^']*)'", match.group("body")))
        out.append(("+".join(keys) if keys else "generic",
                    markup, match.group("colour")))
    return out


class OsLogoTests(TestCase):
    def test_every_branch_was_parsed(self):
        """Guard the guard: a parser that silently matches nothing passes."""
        branches = _branches()
        self.assertGreaterEqual(len(branches), 10, branches)
        keys = "|".join(key for key, _, _ in branches)
        for expected in ("ubuntu", "fedora", "debian", "mint", "rhel",
                         "generic"):
            self.assertIn(expected, keys)

    def test_no_glyph_is_painted_in_its_own_disc_colour(self):
        invisible = []
        for key, markup, disc in _branches():
            if key in _LAYERED:
                continue
            elements = _ELEMENT.findall(markup)
            # elements[0] is the disc itself.
            for element in elements[1:]:
                painted = _FILL.findall(element) + _STROKE.findall(element)
                if painted and all(
                        colour.upper() == disc.upper() for colour in painted):
                    invisible.append(f"{key}: {element}")
        self.assertEqual(
            invisible, [],
            "These glyphs are painted in the same colour as the disc they sit "
            "on, so they render as a flat circle:\n" + "\n".join(invisible))

    def test_thin_strokes_would_vanish_at_sixteen_pixels(self):
        """One viewBox unit is 0.67px at the 16px these render at, so a
        hairline stroke disappears. 1.5 units is the floor."""
        too_thin = []
        for key, markup, _ in _branches():
            for width in re.findall(r'stroke-width="([0-9.]+)"', markup):
                if float(width) < 1.5:
                    too_thin.append(f"{key}: stroke-width={width}")
        self.assertEqual(too_thin, [], "\n".join(too_thin))

    def test_the_detector_catches_a_known_bad_logo(self):
        """The Mint defect, verbatim, before it was fixed."""
        markup = ('<circle cx="12" cy="12" r="10"/>'
                  '<path d="M12 5c-1 3-5 5-5 9z" fill="#87CF3E"/>')
        offenders = [
            element for element in _ELEMENT.findall(markup)[1:]
            if (painted := _FILL.findall(element) + _STROKE.findall(element))
            and all(colour.upper() == "#87CF3E" for colour in painted)]
        self.assertEqual(len(offenders), 1)
