"""Rollout cards: wave columns line up, and the card opens from the keyboard.

The waves page re-renders every five seconds, so both of these are text
properties of the generated markup — the page can only be checked by reading
the template strings the browser will run.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-rollout.js"
CSS = ROOT / "static/css/vigil.css"


def _code_only(src: str) -> str:
    """JS with comments blanked, so a comment that *names* a banned attribute
    isn't reported as the offence. Quotes are kept intact — the banned
    attributes are searched for inside template strings."""
    out = []
    i, n = 0, len(src)
    in_line = in_block = in_str = False
    quote = ""
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
            out.append(ch)
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
        elif in_str:
            out.append(ch)
            if ch == "\\":
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                in_str = False
        elif ch == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
            continue
        elif ch == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
            continue
        else:
            out.append(ch)
            if ch in "\"'`":
                in_str = True
                quote = ch
        i += 1
    return "".join(out)


def _wave_progress_src(code: str) -> str:
    """The body of _waveProgress, up to the next top-level function."""
    start = code.index("function _waveProgress")
    end = code.index("\nfunction ", start + len("function _waveProgress"))
    return code[start:end]


class WaveColumnTests(SimpleTestCase):

    def test_wave_rows_are_a_grid(self):
        wave = _wave_progress_src(_code_only(JS.read_text()))
        for needed in ('class="rlt-wave"', "rlt-wave-tags", "rlt-wave-act"):
            self.assertIn(needed, wave)
        self.assertNotIn("display:flex", wave)

        css = CSS.read_text()
        block = re.search(r"\.rlt-wave \{([^}]*)\}", css)
        self.assertIsNotNone(block, "vigil.css has no .rlt-wave rule")
        self.assertIn(
            "grid-template-columns: 14px minmax(110px, 160px) minmax(0, 1fr) "
            "96px minmax(0, 180px) 92px",
            block.group(1),
        )

        collapse = re.search(
            r"@media \(max-width: 640px\) \{(.*?)\n\}", css, re.DOTALL
        )
        self.assertIsNotNone(collapse, "no 640px collapse for the wave grid")
        self.assertIn(".rlt-wave { grid-template-columns: 14px 1fr auto; }",
                      collapse.group(1))
        self.assertIn(".rlt-wave-tags { display: none; }", collapse.group(1))

    def test_six_cells_always(self):
        """An empty slot collapses the grid to five columns, and everything
        after the gap shifts — the drift this phase exists to stop."""
        wave = _wave_progress_src(_code_only(JS.read_text()))
        self.assertIn(": '<span></span>'", wave)
        self.assertNotIn(": ''}", wave)


class CardKeyboardTests(SimpleTestCase):

    def test_no_inline_handler(self):
        js = _code_only(JS.read_text())
        for forbidden in ("onkeydown=", "onclick="):
            self.assertNotIn(forbidden, js)

        listeners = re.findall(
            r"addEventListener\('keydown'.*?\n\}\);", js, re.DOTALL
        )
        self.assertTrue(listeners, "vigil-rollout.js has no keydown listener")
        # There is more than one keydown listener on this page (the modals'
        # Escape); the card's is the one keyed off the open attribute.
        body = next((b for b in listeners if "[data-rlt-open]" in b), listeners[0])
        self.assertIn("[data-rlt-open]", body)
        self.assertIn("openRolloutDetail(", body)

    def test_card_keeps_its_openable_attributes(self):
        js = JS.read_text()
        self.assertIn('role="button"', js)
        self.assertIn('tabindex="0"', js)
