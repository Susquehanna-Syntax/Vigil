"""The Reports page: the panel exists, the charts fold their colours, and the
stylesheet is still the stylesheet.

QA-42 added Reports as a Deployments sub-tab; it is now its own sidebar page. The page is assembled from
template strings the browser runs, so most of this is a text scan of the JS and
HTML; the two pure functions the charts depend on — the series-colour fold and
the seconds formatter — are run for real, out of the file, with node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-reports.js"
CSS = ROOT / "static/css/vigil.css"
HTML = ROOT / "templates/pages/_reports.html"
PLAYBOOKS_HTML = ROOT / "templates/pages/_playbooks.html"
DASHBOARD = ROOT / "templates/dashboard.html"
BASE = ROOT / "templates/base.html"


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


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


def _node(snippet: str) -> str:
    out = subprocess.run(["node", "-e", snippet], capture_output=True, text=True,
                         timeout=30, check=True)
    return out.stdout


class ReportsTabMarkupTests(SimpleTestCase):

    def test_tab_panel_and_dialog_exist(self):
        html = HTML.read_text()
        for needed in ('id="page-reports"', 'id="rpt-panel"', 'id="rpt-list"',
                       'id="rpt-view"', 'id="rpt-editor-modal"', "js/vigil-reports.js"):
            self.assertIn(needed, html)
        # base.html has someone else's uncommitted edits; the module is loaded
        # by the template that owns the tab, and must stay out of the base.
        self.assertNotIn("vigil-reports", BASE.read_text())

    def test_no_failures_does_not_read_as_nothing_ran(self):
        src = _function(JS.read_text(), "_drawRptWidget")
        self.assertIn("No failures in this range.", src)

    def test_reports_is_its_own_sidebar_page(self):
        self.assertIn('data-page="reports"', BASE.read_text())
        self.assertIn('{% include "pages/_reports.html" %}', DASHBOARD.read_text())
        # No longer a tab under Deployments.
        self.assertNotIn("rpt-panel", PLAYBOOKS_HTML.read_text())
        self.assertNotIn("vigil-reports", PLAYBOOKS_HTML.read_text())
        self.assertIn("pageName !== 'reports'", JS.read_text())

    def test_the_panel_has_a_toolbar_and_a_search_box(self):
        html = HTML.read_text()
        self.assertIn('id="rpt-search"', html)
        self.assertIn('id="rpt-new-btn"', html)
        self.assertIn('id="rpt-editor-overlay"', html)


class ReportsApiTests(SimpleTestCase):

    def test_uses_the_reports_api(self):
        code = _code_only(JS.read_text())
        for needed in ("/api/v1/reports/", "/api/v1/reports/widgets/",
                       "/data/", ".csv"):
            self.assertIn(needed, code)

    def test_the_dialog_opens_by_class_like_every_other_one(self):
        """vigil-modal.js watches the `open` class; a dialog that shows itself
        some other way gets no focus trap and no Escape."""
        code = _code_only(JS.read_text())
        self.assertIn("'rpt-editor-modal').classList.add('open')", code)
        self.assertIn("'rpt-editor-modal')?.classList.remove('open')", code)


class ReportChartFunctionTests(SimpleTestCase):

    def setUp(self):
        self.src = JS.read_text()

    def _prologue(self) -> str:
        tokens = re.search(r"const RPT_SERIES_TOKENS = \[[^\]]*\];", self.src).group(0)
        return "\n".join([tokens,
                          _function(self.src, "rptSeriesColors"),
                          _function(self.src, "rptFormatSeconds")])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_series_colors_fold_past_four(self):
        """Run the real function from the file, not a copy of it. Cycling the
        four tokens is what the CVD-validated order exists to forbid."""
        code = f"{self._prologue()}\n" \
               "console.log(JSON.stringify([rptSeriesColors(3), rptSeriesColors(6)]));"
        three, six = json.loads(_node(code))
        self.assertEqual([c for c in three["colors"]], ["--oc-1", "--oc-2", "--oc-3"])
        self.assertFalse(three["fold"])
        self.assertEqual(six["colors"], ["--oc-1", "--oc-2", "--oc-3", "--oc-4",
                                         "--st-skipped", "--st-skipped"])
        self.assertTrue(six["fold"])

    def test_the_extra_series_fold_into_one_other_series(self):
        code = _code_only(JS.read_text())
        folded = _function(code, "rptFoldedSeries")
        self.assertIn("'Other'", folded)
        self.assertIn("RPT_SERIES_TOKENS.length", folded)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_other_averages_rates_and_sums_counts(self):
        """Six success-rate series: the tail's 50% and 100% are one 75%
        "Other", never a 150% line; a count tail still adds up."""
        code = "\n".join([self._prologue(), _function(self.src, "rptFoldedSeries"),
                          "const S = v => ({ name: 'x', values: v });",
                          "const six = [[10], [20], [30], [40], [50], [100]].map(S);",
                          "const gaps = [[1], [1], [1], [1], [null], [null]].map(S);",
                          "console.log(JSON.stringify([rptFoldedSeries(six, true).pop(),"
                          " rptFoldedSeries(six).pop(), rptFoldedSeries(gaps, true).pop()]));"])
        rate, count, empty = json.loads(_node(code))
        self.assertEqual((rate["name"], rate["values"]), ("Other (average)", [75]))
        self.assertEqual((count["name"], count["values"]), ("Other", [150]))
        self.assertEqual(empty["values"], [None])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_zero_success_rate_is_data(self):
        """A playbook that failed on every host draws a 0% line; it is not
        "Nothing ran in this range". Zero counts are still nothing."""
        code = "\n".join([_function(self.src, "_rptHasData"), "console.log(JSON.stringify(["
                          "_rptHasData({kind: 'success_rate', data: {series: [{values: [null, 0]}]}}),"
                          "_rptHasData({kind: 'success_rate', data: {series: [{values: [null, null]}]}}),"
                          "_rptHasData({kind: 'runs_per_day', data: {ok: [0], failed: [0], pending: [0]}}),"
                          "_rptHasData({kind: 'runs_per_day', data: {ok: [0], failed: [2], pending: [0]}}),"
                          "_rptHasData({kind: 'automation_fires', data: {series: [{values: [0, 0]}]}}),"
                          "]));"])
        self.assertEqual(json.loads(_node(code)), [True, False, False, True, False])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_format_seconds(self):
        code = f"{self._prologue()}\n" \
               "console.log(JSON.stringify([45, 120, 3600, 5400].map(rptFormatSeconds)));"
        self.assertEqual(json.loads(_node(code)), ["45s", "2m", "1h", "1.5h"])


class ReportChartLifecycleTests(SimpleTestCase):

    def test_charts_are_destroyed_before_redraw(self):
        """A Chart.js instance left alive over a removed canvas leaks, and the
        report view redraws on every open."""
        code = _code_only(JS.read_text())
        self.assertIn("_rptCharts", code)
        self.assertIn("c.destroy()", _function(code, "_destroyRptCharts"))
        self.assertIn("_destroyRptCharts()", _function(code, "openReport"))
        self.assertIn("_destroyRptCharts()", _function(code, "_showReportList"))


class ReportHeaderTests(SimpleTestCase):

    def test_the_range_reads_as_a_number_of_days(self):
        """`range.days` is the list of dates; the header counts it rather than
        printing every date between "last" and "days"."""
        code = _code_only(JS.read_text())
        self.assertIn("(range.days || []).length", _function(code, "openReport"))
        self.assertNotIn("String(range.days ||", code)


class ReportSafetyTests(SimpleTestCase):

    def test_no_inline_handlers_and_escaped(self):
        code = _code_only(JS.read_text())
        self.assertNotIn("onclick=", code)
        self.assertNotIn("onchange=", code)
        self.assertIn("escHtml(", code)
        self.assertIn("escAttr(", code)


class ReportStyleTests(SimpleTestCase):

    def test_css_appended_not_replaced(self):
        """A previous phase overwrote vigil.css with its ten new lines."""
        css = CSS.read_text()
        self.assertGreater(len(css.splitlines()), 4500)
        self.assertIn(".modal {", css)
        self.assertIn(".fcn-if {", css)
        self.assertIn(".rpt-grid", css)

    def test_no_side_stripe(self):
        css = CSS.read_text()
        rules = "\n".join(re.findall(r"\.rpt-[\w-]*\s*\{[^}]*\}", css))
        for banned in ("border-left", "border-right"):
            self.assertNotIn(banned, rules)

    def test_the_chart_has_a_boxed_height_and_the_grid_collapses(self):
        css = CSS.read_text()
        self.assertRegex(css, r"\.rpt-chart \{ height: 220px; position: relative; \}")
        self.assertRegex(css, r"\.rpt-picks \{[^}]*max-height: 180px; overflow-y: auto")
        self.assertRegex(
            css,
            r"@media \(max-width: 640px\) \{[^}]*\.rpt-grid \{ "
            r"grid-template-columns: minmax\(0, 1fr\); \}",
        )
