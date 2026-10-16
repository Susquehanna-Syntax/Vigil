"""Container logs read like a terminal, not a wall of text (QA-49).

termHtml / termPlain are pure and run for real in node, cut out of the shipped
vigil-utils.js; the logs dialog is a text scan of vigil-containers.js.
"""

import json
import re
import shutil
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.reports.test_reports_ui import _code_only, _function, _node

ROOT = Path(__file__).resolve().parents[2]
UTILS = (ROOT / "static/js/vigil-utils.js").read_text()
CONTAINERS = _code_only((ROOT / "static/js/vigil-containers.js").read_text())
CSS = (ROOT / "static/css/vigil.css").read_text()

TERM_FUNCS = ("termNewState", "_termSgr", "_termClasses", "_termOverwrite", "_termLineHtml",
              "termHtml", "termPlain", "escAttr")
TERM_CONSTS = "\n".join(re.search(rf"^const {name} = .*;$", UTILS, re.M).group(0)
                        for name in ("_TERM_TS", "_TERM_ESC", "_TERM_CTRL", "_TERM_COLOURS"))

ESC = "\x1b"
TS = "2026-10-16T18:04:05.123456789Z "


def _term(call: str):
    code = "\n".join([
        # escHtml goes through the DOM in the browser; this is what it does.
        "const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')"
        ".replace(/>/g, '&gt;');",
        TERM_CONSTS, *(_function(UTILS, n) for n in TERM_FUNCS),
        f"console.log(JSON.stringify({call}));"])
    return json.loads(_node(code))


def _html(text: str, opts: dict | None = None) -> str:
    return _term(f"termHtml({json.dumps(text)}, {json.dumps(opts or {})})")


@skipUnless(shutil.which("node"), "node is not installed")
class TermHtmlTests(SimpleTestCase):

    def test_colours_become_spans_and_reset_ends_them(self):
        html = _html(f"{ESC}[32mINFO{ESC}[0m started on :8080")
        self.assertEqual(html, '<span class="term-g">INFO</span> started on :8080')

    def test_bright_bold_and_256_colour(self):
        self.assertEqual(_html(f"{ESC}[1;91mERR{ESC}[22;39m ok"),
                         '<span class="term-br term-bold">ERR</span> ok')
        self.assertEqual(_html(f"{ESC}[38;5;3mwarn{ESC}[m"), '<span class="term-y">warn</span>')
        self.assertEqual(_html(f"{ESC}[38;2;10;20;30mtrue{ESC}[0m"), "true")

    def test_log_text_is_never_markup(self):
        html = _html(f'{ESC}[31m<img src=x onerror="alert(1)">{ESC}[0m & done')
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x", html)
        self.assertIn("&amp; done", html)

    def test_other_escapes_and_controls_are_dropped(self):
        self.assertEqual(_html(f"{ESC}[2K{ESC}[1Gline{ESC}]0;title\x07 end\x08\x00{ESC}"), "line end")

    def test_tabs_and_spacing_stay_as_written(self):
        self.assertEqual(_html("a\tb    c"), "a\tb    c")

    def test_a_carriage_return_overwrites_the_line(self):
        self.assertEqual(_html("10%\r50%\r100% done"), "100% done")
        self.assertEqual(_html("windows line\r"), "windows line")

    def test_colour_carries_across_lines_and_calls(self):
        self.assertEqual(_html(f"{ESC}[33mone\ntwo{ESC}[0m\nthree"),
                         '<span class="term-y">one</span>\n<span class="term-y">two</span>\nthree')
        self.assertEqual(_term(f"(() => {{ const s = termNewState();"
                               f" termHtml({json.dumps(ESC + '[36mA')}, {{}}, s);"
                               " return termHtml('B', {}, s); })()"),
                         '<span class="term-c">B</span>')

    def test_docker_timestamps_off_time_or_left_alone(self):
        self.assertEqual(_html(TS + "ready", {"timestamps": "off"}), "ready")
        self.assertRegex(_html(TS + "ready", {"timestamps": "time"}),
                         r'^<span class="term-ts" title="2026-10-16 18:04:05\.123456789 UTC">'
                         r"18:04:05</span> ready$")
        self.assertEqual(_html(TS + "ready"), TS + "ready")

    def test_plain_text_for_copying(self):
        text = f"{TS}{ESC}[32mINFO{ESC}[0m up\n{TS}50%\r100%"
        self.assertEqual(_term(f"termPlain({json.dumps(text)}, {{timestamps: 'off'}})"),
                         "INFO up\n100%")
        self.assertEqual(_term(f"termPlain({json.dumps(text)}, {{timestamps: 'time'}})"),
                         "18:04:05 INFO up\n18:04:05 100%")


class LogDialogTests(SimpleTestCase):

    def test_the_dialog_is_resizable_and_the_log_fills_it(self):
        els = _function(CONTAINERS, "_logEls")
        self.assertIn("modal modal-wide ctr-logs-modal", els)
        self.assertIn(".modal.ctr-logs-modal { display: flex; flex-direction: column;", CSS)
        self.assertIn(".ctr-logs-modal .ctr-logs { flex: 1 1 auto;", CSS)

    def test_no_soft_wrap_unless_asked(self):
        self.assertIn('class="ctr-logs term"', _function(CONTAINERS, "_logEls"))
        term = re.search(r"^\.term \{[^}]*\}", CSS, re.M).group(0)
        self.assertIn("white-space: pre;", term)
        self.assertIn("tab-size: 8", term)
        self.assertIn(".term.term-wrap { white-space: pre-wrap;", CSS)
        self.assertIn("term-wrap", _function(CONTAINERS, "_logEls"))

    def test_lines_are_rendered_through_termhtml(self):
        self.assertIn("termHtml(", _function(CONTAINERS, "_appendLogLines"))
        self.assertIn("termHtml(", _function(CONTAINERS, "_renderLogLines"))
        poll = _function(CONTAINERS, "_pollContainerLogs")
        self.assertNotIn("textContent +=", poll)
        self.assertIn("_appendLogLines(", poll)

    def test_copy_takes_the_plain_text(self):
        self.assertIn("termPlain(", _function(CONTAINERS, "_copyLogLines"))
        self.assertIn("navigator.clipboard.writeText", _function(CONTAINERS, "_copyLogLines"))

    def test_scrollback_is_capped(self):
        self.assertIn("LOG_VIEW_MAX", _function(CONTAINERS, "_appendLogLines"))

    def test_run_output_uses_the_same_renderer(self):
        for name, cls in (("vigil-runhistory.js", "run-task-out"), ("vigil-charts.js", "host-run-out"),
                          ("vigil-playbooks.js", "blf-output")):
            src = (ROOT / "static/js" / name).read_text()
            self.assertIn(f'class="{cls} term term-wrap">${{', src, name)
            self.assertIn("termHtml(", src, name)
