"""Rollout start dialog and batch lanes: what the browser will run.

The start dialog picks several ladders and the list draws the batch that start
returns as one card with a lane per group. The page re-renders from template
strings every five seconds, so the checkable properties are the markup those
strings produce and the payload the dialog POSTs.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-rollout.js"
CSS = ROOT / "static/css/vigil.css"
BASE = ROOT / "templates/base.html"


def _code_only(src: str) -> str:
    """JS with comments blanked, so a comment that *names* a banned attribute
    isn't reported as the offence. Quotes are kept intact — the tokens are
    searched for inside template strings."""
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


def _function(code: str, name: str) -> str:
    """The named function's source, up to the next top-level ``function``."""
    start = code.index(f"function {name}")
    end = code.find("\nfunction ", start + len(f"function {name}"))
    return code[start:] if end == -1 else code[start:end]


class StartDialogTests(SimpleTestCase):

    def test_start_picks_several_groups(self):
        code = _code_only(JS.read_text())
        for needed in ("/api/v1/wave-groups/", "data-rlt-group", "aria-pressed",
                       "wave_group_tags"):
            self.assertIn(needed, code)
        self.assertNotIn("getElementById('rollout-start-group')", code)

        base = BASE.read_text()
        self.assertIn('id="rollout-start-groups"', base)
        self.assertNotIn('id="rollout-start-group"', base)

    def test_payload_follows_the_number_of_picks(self):
        """No pick = every enabled wave, one = that ladder, two or more = a
        parallel batch. Always sending wave_group_tag is the bug: the second
        group on would be dropped and one ladder started alone."""
        submit = _function(_code_only(JS.read_text()), "submitRolloutStart")
        self.assertIn("wave_group_tag: picked[0]", submit)
        self.assertIn("wave_group_tags: picked.slice()", submit)


class BatchLaneTests(SimpleTestCase):

    def test_batches_draw_as_lanes(self):
        code = _code_only(JS.read_text())
        self.assertIn("function _batchCard(", code)
        card = _function(code, "_batchCard")
        self.assertIn("rlt-lanes", card)
        self.assertIn("rlt-lane", card)
        self.assertIn("r.batch", _function(code, "_groupRollouts"))
        # The card itself must not open a rollout — a batch has N of them to
        # open, and each lane head carries its own data-rlt-open.
        self.assertNotIn("data-rlt-open", card)
        self.assertIn("data-rlt-open", _function(code, "_batchLane"))

    def test_halt_all_uses_the_batch_endpoint_once(self):
        code = _code_only(JS.read_text())
        self.assertIn("/api/v1/rollouts/batch/", code)
        self.assertIn("halt-batch", code)
        self.assertEqual(1, _function(code, "confirmRolloutAction")
                         .count("/api/v1/rollouts/batch/"))


class LaneStyleTests(SimpleTestCase):

    def _lane_rules(self, css: str) -> str:
        return "\n".join(re.findall(r"\.rlt-lane[\w-]*\s*\{[^}]*\}", css))

    def test_lanes_have_no_side_stripe(self):
        rules = self._lane_rules(CSS.read_text())
        # tinted as a whole surface (--s2: the card itself is --s1, so a --s1
        # lane would not show against it)
        self.assertRegex(rules, r"background: var\(--s\d\)")
        for banned in ("border-left", "border-right"):
            self.assertNotIn(banned, rules)

    def test_lanes_stack_under_640px(self):
        css = CSS.read_text()
        self.assertIn(".rlt-lanes { display: grid; grid-template-columns: "
                      "repeat(auto-fit, minmax(260px, 1fr));", css)
        self.assertRegex(
            css,
            r"@media \(max-width: 640px\) \{[^}]*\.rlt-lanes \{ "
            r"grid-template-columns: minmax\(0, 1fr\); \}",
        )
