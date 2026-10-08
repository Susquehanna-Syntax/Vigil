"""The run-detail results ring sorts its group to the top; a host's name opens
its run detail.

Both are browser behaviour the suite cannot execute, so — like the other UI
QA phases — these read the shipped JavaScript and CSS as text and pin the
mechanism that would otherwise regress silently: the ring must *sort* (the old
handler hid the other cards, which is the bug the user reported), slices must
be clickable, and the host detail must not resurrect the nonce or signature
the API deliberately stopped exposing.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
CHARTS_JS = ROOT / "static/js/vigil-charts.js"
TASKS_JS = ROOT / "static/js/vigil-tasks.js"
RUNHISTORY_JS = ROOT / "static/js/vigil-runhistory.js"
CSS = ROOT / "static/css/vigil.css"


def _source(path, start_marker):
    """The named function's source, from its `name(` to the end of the file or
    the next top-level `function `, whichever comes first.

    Matched on the opening paren, not the bare name: a preceding comment that
    quotes the function must not join the extract (one says "no nonce" — a
    forbidden-substring test would read that as an offence).
    """
    text = path.read_text()
    start = text.index(start_marker)
    rest = text[start + len(start_marker):]
    nxt = rest.find("\nfunction ")
    end = len(rest) if nxt == -1 else nxt
    return text[start:start + len(start_marker) + end]


def _host_run_sources():
    """`openHostRunDetail` plus the helpers that render each of its blocks."""
    return (_source(CHARTS_JS, "function openHostRunDetail(")
            + _source(CHARTS_JS, "function _hostRunTaskHtml("))


class RingSortsTests(SimpleTestCase):
    def test_ring_sorts_instead_of_hiding(self):
        src = _source(CHARTS_JS, "function wireRunSummary(")
        self.assertNotIn(".hidden =", src,
                         "the ring still hides the other host cards instead of "
                         "sorting its group to the top")
        for needed in ("run-sort-dim", "data-order", "getBoundingClientRect"):
            self.assertIn(needed, src)

    def test_slices_are_clickable(self):
        src = _source(CHARTS_JS, "function wireRunSummary(")
        self.assertIn(".donut-slice", src)
        css = CSS.read_text()
        rule = re.search(r"\.donut-slice\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("cursor: pointer", rule.group(1))

    def test_reduced_motion_respected(self):
        src = _source(CHARTS_JS, "function wireRunSummary(")
        self.assertIn("prefers-reduced-motion", src)


class HostRunDetailTests(SimpleTestCase):
    def test_host_detail_exists(self):
        src = _host_run_sources()
        self.assertIn("String(t.host) === String(hostId)", src)
        self.assertIn("escHtml(JSON.stringify(", src)

    def test_both_dialogs_link_hosts(self):
        for path in (TASKS_JS, RUNHISTORY_JS):
            text = path.read_text()
            self.assertIn("data-host-run", text, path.name)
            self.assertRegex(
                text, r"wireRunSummary\([^)]*,[^)]*,\s*run\)",
                f"{path.name} does not pass the run to wireRunSummary")

    def test_no_nonce_or_signature(self):
        src = _host_run_sources()
        for forbidden in ("nonce", "signature"):
            self.assertNotIn(forbidden, src,
                             f"the host detail exposes {forbidden}, which the "
                             "API deliberately does not return")
