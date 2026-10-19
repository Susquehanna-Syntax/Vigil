"""The report builder edits each chart's data, type, labels, legend and sources,
with a live preview drawn from real run history (QA-48).

The draft helpers are pure and run for real in node, out of the shipped file;
the markup and wiring are a text scan of it.
"""

import json
import re
import shutil
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from .test_reports_ui import _code_only, _function, _node

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-reports.js"
CSS = ROOT / "static/css/vigil.css"
TEMPLATE = ROOT / "templates/pages/_reports.html"

SRC = JS.read_text()
CODE = _code_only(SRC)

#: What the module reads from vigil-utils.js and the page, stubbed for node.
STUBS = "\n".join([
    "const tok = n => n;",
    "const cssVar = n => n;",
    "const escHtml = s => String(s);",
    "const escAttr = escHtml;",
    "const document = { addEventListener() {} };",
    "const window = {};",
])

#: Enough of the catalogue for the draft helpers: each entry's default chart
#: and the charts it allows, default first, as /api/v1/reports/widgets/ has them.
CATALOG = [
    {"kind": "success_rate", "chart": "line", "charts": ["line", "area", "bar", "table"]},
    {"kind": "runs_per_day", "chart": "stacked-bar",
     "charts": ["stacked-bar", "bar", "line", "area", "table"]},
    {"kind": "duration", "chart": "line", "charts": ["line", "area", "bar", "table"]},
    {"kind": "failures_by_step", "chart": "bar",
     "charts": ["bar", "column", "pie", "doughnut", "table"]},
]

UUID = "0b9f6a52-3f4e-4f7a-9d1c-6a2e8f1b7c30"


def _run(snippet: str):
    """*snippet*, run in node after the stubs and the whole shipped module.
    Sets are written out as sorted lists so a draft compares as JSON."""
    replacer = "(k, v) => v instanceof Set ? [...v].sort() : v"
    return json.loads(_node("\n".join([
        STUBS, SRC, f"const CATALOG = {json.dumps(CATALOG)};",
        f"const out = (() => {{ {snippet} }})();",
        f"console.log(JSON.stringify(out, {replacer}));"])))


def _round_trip(spec: dict):
    return _run(f"return rptSpecFromDraft(rptDraftFrom({json.dumps(spec)}, CATALOG));")


BLANK = ("{kind: 'runs_per_day', chart: 'line', title: '  ', xLabel: '', yLabel: '', axes: true,"
         " legend: 'auto', sourceMode: 'all', sources: new Set(), place: null}")


@skipUnless(shutil.which("node"), "node is not installed")
class ReportDraftTests(SimpleTestCase):

    def test_draft_round_trip(self):
        full = {"kind": "runs_per_day", "chart": "line", "title": "Hosts a day",
                "x_label": "When", "y_label": "Hosts", "legend": False,
                "sources": [UUID], "x": 0, "y": 4, "w": 6, "h": 4}
        self.assertEqual(_round_trip(full), full)
        self.assertEqual(_round_trip({"kind": "success_rate", "chart": "line"}),
                         {"kind": "success_rate", "chart": "line"})
        self.assertEqual(_round_trip({"kind": "success_rate", "chart": "line",
                                      "x_label": "", "y_label": ""}),
                         {"kind": "success_rate", "chart": "line", "x_label": "", "y_label": ""})

    def test_a_spec_without_a_chart_gets_the_catalogue_default(self):
        self.assertEqual(_round_trip({"kind": "failures_by_step"}),
                         {"kind": "failures_by_step", "chart": "bar"})

    def test_spec_sends_only_what_is_set(self):
        self.assertEqual(_run(f"return rptSpecFromDraft({BLANK});"),
                         {"kind": "runs_per_day", "chart": "line"})
        self.assertNotIn("x_label", _run(
            f"return rptSpecFromDraft({{...{BLANK}, kind: 'failures_by_step', chart: 'pie',"
            " xLabel: 'Steps'});"))
        self.assertNotIn("sources", _run(
            f"return rptSpecFromDraft({{...{BLANK}, sourceMode: 'some'}});"))
        self.assertNotIn("sources", _run(
            f"return rptSpecFromDraft({{...{BLANK}, sources: new Set(['{UUID}'])}});"),
            "a chart set to all of the report's sources sends none of its own")

    def test_titles_off_sends_both_blank(self):
        self.assertEqual(_run(
            f"return rptSpecFromDraft({{...{BLANK}, axes: false, xLabel: 'kept'}});"),
            {"kind": "runs_per_day", "chart": "line", "x_label": "", "y_label": ""})

    def test_kind_change_keeps_a_fitting_chart(self):
        start = ("{kind: 'success_rate', chart: 'line', xLabel: 'x', yLabel: 'y', title: 'Mine',"
                 " place: {x: 1, y: 2, w: 3, h: 4}}")
        to_rows = _run(f"return rptKindChanged({start}, CATALOG[3]);")
        self.assertEqual((to_rows["kind"], to_rows["chart"], to_rows["xLabel"], to_rows["yLabel"]),
                         ("failures_by_step", "bar", "", ""))
        self.assertEqual(to_rows["place"], {"x": 1, "y": 2, "w": 3, "h": 4})
        self.assertEqual(to_rows["title"], "Mine")
        self.assertEqual(_run(f"return rptKindChanged({start}, CATALOG[2]);")["chart"], "line")

    def test_chart_labels(self):
        self.assertEqual(_run("return [rptChartLabel('bar', 'days'), rptChartLabel('bar', 'rows'),"
                              " rptChartLabel('stacked-bar', 'days'), rptHasAxes('pie'),"
                              " rptHasAxes('table'), rptHasAxes('column')];"),
                         ["Bars", "Bars across", "Stacked bars", False, False, True])


class ReportEditorMarkupTests(SimpleTestCase):

    def test_editor_markup_and_wiring(self):
        for needed in ("data-rpt-wkind", "data-rpt-wchart", "data-rpt-waxes", "data-rpt-wx",
                       "data-rpt-wy", "data-rpt-wlegend", "data-rpt-wsrcmode", "data-rpt-wsrc",
                       "data-rpt-wdup", "data-rpt-wprev", "data-rpt-wtitle",
                       "/api/v1/reports/preview/", "_rptPreviewSeq", "_rptPreviewCharts"):
            self.assertIn(needed, CODE)
        self.assertIn("_destroyRptPreview()", _function(CODE, "closeRptEditor"))
        self.assertNotIn("({ kind: w.kind, title: w.title || '' })", _function(CODE, "openReportEditor"))
        self.assertIn("rptSpecFromDraft", _function(CODE, "_rptBodyFromForm"))

    def test_typing_never_redraws_the_cards(self):
        """A redraw replaces the input being typed in, and the cursor with it."""
        wiring = CODE[CODE.index("document.addEventListener('DOMContentLoaded'"):]
        typing = wiring[wiring.index("addEventListener('input', (e) => {\n    const t = e.target;"):]
        typing = typing[:typing.index("});")]
        self.assertIn("_rptSchedulePreview()", typing)
        self.assertNotIn("_renderRptWidgets", typing)

    def test_a_stale_preview_is_dropped(self):
        body = _function(CODE, "_rptRunPreview")
        self.assertIn("const seq = ++_rptPreviewSeq", body)
        self.assertIn("if (seq !== _rptPreviewSeq) return;", body)
        self.assertIn("_rptPreviewCharts", body)

    def test_the_preview_charts_are_its_own(self):
        """The viewer's charts and the preview's never destroy each other."""
        draw = _function(CODE, "_drawRptWidget")
        self.assertIn("charts = _rptCharts", draw)
        self.assertIn("charts.push(chart)", draw)
        self.assertNotIn("_rptCharts", _function(CODE, "_destroyRptPreview"))

    def test_template_and_css(self):
        html = TEMPLATE.read_text()
        self.assertIn('id="rpt-widgets"', html)
        self.assertIn('id="rpt-widget-add"', html)
        self.assertRegex(html, r'class="modal modal-wide rpt-editor-modal" id="rpt-editor-modal"')
        css = CSS.read_text()
        self.assertIn("QA-48: report editor", css)
        self.assertIn(".rpt-wcard", css)
        self.assertIn(".rpt-wprev", css)
        self.assertGreater(len(css.splitlines()), 4586)
        rules = "\n".join(re.findall(r"\.rpt-[\w-]*[^{]*\{[^}]*\}", css))
        for banned in ("border-left", "border-right"):
            self.assertNotIn(banned, rules)
