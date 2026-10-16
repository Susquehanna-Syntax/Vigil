"""The report viewer is a grid of cards you can drag and resize.

The grid setup is shared with the Dashboard rather than copied (QA-47), and
every card draws the chart, the axis titles and the legend its widget asks for.
The page is built from template strings the browser runs, so most of this is a
text scan of the shipped JS; the chart building, the outcome counting and the
layout merge are pure functions and run for real, out of the file, with node.
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
DASHBOARDS_JS = ROOT / "static/js/vigil-dashboards.js"
CSS = ROOT / "static/css/vigil.css"

SRC = JS.read_text()
CODE = _code_only(SRC)

#: What the module reads from vigil-utils.js and the page, stubbed for node. In
#: node a token is its own name, which is how the assertions below are written.
#: The whole shipped file runs after these, so every table and helper a chart
#: builder reaches is the real one, never a copy that can drift.
STUBS = "\n".join([
    "const tok = n => n;",
    "let cssVar = n => n;",
    "const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');",
    "const escAttr = escHtml;",
    "const document = { addEventListener() {} };",
    "const window = {};",
])

#: A days widget with three days and three series, and a rows widget with six
#: points: enough for a legend to be worth showing and more than a pie's fill.
DAYS_WIDGET = {
    "kind": "runs_per_day", "title": "Runs per day",
    "x_label": "Day", "y_label": "Host-runs", "legend": None,
    "data": {"days": ["2026-10-14", "2026-10-15", "2026-10-16"],
             "ok": [3, 4, 5], "failed": [1, 0, 2], "pending": [0, 1, 0]},
}
ROWS_WIDGET = {
    "kind": "failures_by_step", "title": "Failures by step",
    "x_label": "Failures", "y_label": "Step", "legend": None,
    "data": {"rows": [{"source": "Patch web", "step": f"step-{i}", "failed": 7 - i}
                      for i in range(6)]},
}


def _widget(chart: str, widget: dict) -> dict:
    return {**widget, "chart": chart}


def _run(snippet: str):
    """*snippet*, run in node after the stubs and the whole shipped module."""
    return json.loads(_node("\n".join([STUBS, SRC, snippet])))


def _config(widget: dict):
    """rptChartConfig for *widget*, through node. null comes back as None."""
    return _run(f"console.log(JSON.stringify(rptChartConfig({json.dumps(widget)})));")


class SharedGridHelperTests(SimpleTestCase):
    """Both pages build their grid through one pair of helpers, so a change to
    the row height or the renderCB override lands on both at once."""

    def test_dashboards_js_defines_the_helpers(self):
        dash = _code_only(DASHBOARDS_JS.read_text())
        for name in ("gridBaseOptions", "gridMake", "gridLayoutOf"):
            self.assertIn(f"function {name}(", dash)

    def test_the_dashboard_builds_its_grid_with_the_helper(self):
        dash = _code_only(DASHBOARDS_JS.read_text())
        self.assertIn("gridMake(", _function(dash, "_dashRender"))

    def test_gridmake_carries_the_rendercb_override(self):
        """The library's own default writes content as text; whoever sets it
        must be the helper, or the page that renders first shows markup."""
        dash = _code_only(DASHBOARDS_JS.read_text())
        self.assertIn("GridStack.renderCB", _function(dash, "gridMake"))

    def test_geometry_is_read_from_the_live_nodes(self):
        body = _function(_code_only(DASHBOARDS_JS.read_text()), "gridLayoutOf")
        self.assertIn("getGridItems()", body)
        self.assertIn("gridstackNode", body)
        self.assertNotIn(".save(", body)

    def test_reports_uses_the_helpers_and_never_the_library_directly(self):
        self.assertIn("gridMake(", CODE)
        self.assertIn("gridLayoutOf(", CODE)
        self.assertNotIn("GridStack.init(", CODE)


class ReportChartTypeTests(SimpleTestCase):
    """The widget says how it wants to be drawn and the chart obeys, whatever
    family its data is in."""

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_line(self):
        cfg = _config(_widget("line", DAYS_WIDGET))
        self.assertEqual(cfg["type"], "line")
        for dataset in cfg["data"]["datasets"]:
            self.assertFalse(dataset["fill"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_area(self):
        cfg = _config(_widget("area", DAYS_WIDGET))
        self.assertEqual(cfg["type"], "line")
        self.assertEqual([d["fill"] for d in cfg["data"]["datasets"]], ["origin"] * 3)
        self.assertEqual(_run("console.log(JSON.stringify(rptAlpha('#102030', 0.25)));"),
                         "rgba(16, 32, 48, 0.25)")
        # tok() is a function in the browser, so the fill must apply its alpha
        # to the token's value when drawn, not to the function.
        fill = _run("\n".join([
            "cssVar = () => '#102030';",
            f"const w = {json.dumps(_widget('area', DAYS_WIDGET))};",
            "console.log(JSON.stringify(rptChartConfig(w).data.datasets[0].backgroundColor()));"]))
        self.assertEqual(fill, "rgba(16, 32, 48, 0.25)")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_bar_over_days_is_not_stacked(self):
        cfg = _config(_widget("bar", DAYS_WIDGET))
        self.assertEqual(cfg["type"], "bar")
        self.assertNotEqual(cfg["options"]["scales"]["x"].get("stacked"), True)
        self.assertNotEqual(cfg["options"]["scales"]["y"].get("stacked"), True)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_stacked_bar(self):
        cfg = _config(_widget("stacked-bar", DAYS_WIDGET))
        self.assertEqual(cfg["type"], "bar")
        self.assertIs(cfg["options"]["scales"]["x"]["stacked"], True)
        self.assertIs(cfg["options"]["scales"]["y"]["stacked"], True)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_table_builds_no_chart(self):
        self.assertIsNone(_config(_widget("table", DAYS_WIDGET)))
        self.assertIsNone(_config(_widget("table", ROWS_WIDGET)))

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_days_widget_is_never_a_pie(self):
        """The server would not ask, and a pie over dates is meaningless. An
        unknown chart draws nothing either."""
        for chart in ("pie", "doughnut", "column", "nonsense"):
            with self.subTest(chart=chart):
                self.assertIsNone(_config(_widget(chart, DAYS_WIDGET)))

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_rows_bar_runs_across(self):
        cfg = _config(_widget("bar", ROWS_WIDGET))
        self.assertEqual(cfg["options"]["indexAxis"], "y")
        self.assertEqual(len(cfg["data"]["labels"]), 6)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_rows_column_runs_up(self):
        cfg = _config(_widget("column", ROWS_WIDGET))
        self.assertEqual(cfg["options"]["indexAxis"], "x")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_rows_pie_folds_the_tail_into_other(self):
        for chart in ("pie", "doughnut"):
            cfg = _config(_widget(chart, ROWS_WIDGET))
            with self.subTest(chart=chart):
                self.assertEqual(cfg["type"], chart)
                self.assertIsNone(cfg["options"].get("scales"))
                labels = cfg["data"]["labels"]
                self.assertEqual(len(labels), 5)
                self.assertEqual(labels[-1], "Other")
                self.assertEqual(cfg["data"]["datasets"][0]["data"], [7, 6, 5, 4, 5])
        # Chart.js never calls functions inside a colour array, so the wedge
        # colours are one function of the wedge's index.
        colours = _run("\n".join([
            f"const w = {json.dumps(_widget('pie', ROWS_WIDGET))};",
            "const bg = rptChartConfig(w).data.datasets[0].backgroundColor;",
            "console.log(JSON.stringify([typeof bg, [0, 4].map(i => bg({dataIndex: i}))]));"]))
        self.assertEqual(colours, ["function", ["--oc-1", "--st-skipped"]])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_short_rows_widget_has_no_other_slice(self):
        widget = {**ROWS_WIDGET,
                  "data": {"rows": [{"source": "Patch web", "step": f"s{i}", "failed": i}
                                    for i in range(1, 4)]}}
        self.assertEqual(_config(_widget("pie", widget))["data"]["labels"],
                         ["Patch web · s3", "Patch web · s2", "Patch web · s1"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_days_value_axis_keeps_its_units(self):
        """A rate is a percentage held to 0–100; counts start at zero and are
        whole. Which chart they are drawn in changes none of that."""
        rate = _config(_widget("bar", {
            **DAYS_WIDGET, "kind": "success_rate",
            "data": {"days": ["2026-10-15"],
                     "series": [{"name": "Patch web", "values": [80]}]}}))
        self.assertEqual((rate["options"]["scales"]["y"]["min"],
                          rate["options"]["scales"]["y"]["max"]), (0, 100))
        counts = _config(_widget("bar", DAYS_WIDGET))["options"]["scales"]["y"]
        self.assertIs(counts["beginAtZero"], True)
        self.assertIs(counts["ticks"]["precision"], 0)


class ReportAxisAndLegendTests(SimpleTestCase):
    """The widget's own titles and legend choice, as drawn."""

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_title_shows_only_when_there_is_one(self):
        cfg = _config(_widget("line", {**DAYS_WIDGET, "x_label": "Day", "y_label": ""}))
        x, y = cfg["options"]["scales"]["x"]["title"], cfg["options"]["scales"]["y"]["title"]
        self.assertTrue(x["display"])
        self.assertEqual(x["text"], "Day")
        self.assertFalse(y["display"])
        self.assertEqual(y["text"], "")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_titles_are_drawn_where_the_server_put_them(self):
        """x_label is the horizontal axis as drawn. The server turns a rows
        widget's labels round for columns (default_labels); turning them again
        here would put the step names on the value axis."""
        across = _config(_widget("bar", ROWS_WIDGET))["options"]["scales"]
        up = _config(_widget("column", {**ROWS_WIDGET, "x_label": "Step",
                                        "y_label": "Failures"}))["options"]["scales"]
        self.assertEqual((across["x"]["title"]["text"], across["y"]["title"]["text"]),
                         ("Failures", "Step"))
        self.assertEqual((up["x"]["title"]["text"], up["y"]["title"]["text"]),
                         ("Step", "Failures"))

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_legend_false_hides_it_however_many_series(self):
        cfg = _config(_widget("line", {**DAYS_WIDGET, "legend": False}))
        self.assertEqual(len(cfg["data"]["datasets"]), 3)
        self.assertFalse(cfg["options"]["plugins"]["legend"]["display"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_legend_null_shows_it_only_when_there_is_something_to_name(self):
        one = {**DAYS_WIDGET, "kind": "success_rate", "legend": None,
               "data": {"days": ["2026-10-15"],
                        "series": [{"name": "Patch web", "values": [80]}]}}
        self.assertFalse(_config(_widget("line", one))["options"]["plugins"]["legend"]["display"])
        three = _config(_widget("line", {**DAYS_WIDGET, "legend": None}))
        self.assertTrue(three["options"]["plugins"]["legend"]["display"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_pie_always_names_its_wedges(self):
        cfg = _config(_widget("pie", {**ROWS_WIDGET, "legend": None}))
        self.assertEqual(len(cfg["data"]["datasets"]), 1)
        self.assertTrue(cfg["options"]["plugins"]["legend"]["display"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_title_colour_is_a_colour_not_a_function(self):
        """Chart.js takes a plain string where a title colour goes; a
        scriptable option there is not a colour and draws as none."""
        cfg = _config(_widget("line", {**DAYS_WIDGET, "x_label": "Day"}))
        self.assertIsInstance(cfg["options"]["scales"]["x"]["title"]["color"], str)


class ReportRowPointsTests(SimpleTestCase):

    def _points(self, widget):
        return _run(f"console.log(JSON.stringify(rptRowPoints({json.dumps(widget)})));")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_host_outcomes_count_rows_by_outcome_in_order(self):
        """Outcomes come back in the order the tables list them, whatever
        order the rows were in, and the ones nobody has are left out."""
        self.assertEqual(self._points({
            "kind": "host_outcomes", "title": "Host outcomes", "chart": "pie",
            "data": {"rows": [
                {"host": "api", "source": "Patch web", "outcome": "pending", "at": None},
                {"host": "web", "source": "Patch web", "outcome": "ok", "at": None},
                {"host": "db", "source": "Patch web", "outcome": "failed", "at": None},
                {"host": "cache", "source": "Patch web", "outcome": "ok", "at": None},
            ]}}),
            [{"label": "ok", "value": 2},
             {"label": "failed", "value": 1},
             {"label": "pending", "value": 1}])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_step_points_name_their_source(self):
        self.assertEqual(self._points({
            "kind": "failures_by_step",
            "data": {"rows": [{"source": "Patch web", "step": "apt", "failed": 3}]}}),
            [{"label": "Patch web · apt", "value": 3}])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_branch_points_name_their_source(self):
        self.assertEqual(self._points({
            "kind": "branch_paths",
            "data": {"rows": [{"source": "Patch web", "branch": "if patchy", "hosts": 2}]}}),
            [{"label": "Patch web · if patchy", "value": 2}])


class ReportLayoutMergeTests(SimpleTestCase):

    def _merge(self, specs, layout):
        return _run("\n".join([
            f"const specs = {json.dumps(specs)};",
            f"const layout = {json.dumps(layout)};",
            ("console.log(JSON.stringify("
             "[rptMergeLayout(specs, layout), specs]));"),
        ]))

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_place_goes_into_the_spec_it_belongs_to(self):
        merged, specs = self._merge([{"kind": "a"}, {"kind": "b", "title": "t"}],
                                    [{"id": "1", "x": 4, "y": 0, "w": 8, "h": 5}])
        self.assertEqual(merged[0], {"kind": "a"})
        self.assertEqual(merged[1], {"kind": "b", "title": "t", "x": 4, "y": 0, "w": 8, "h": 5})
        self.assertEqual(specs, [{"kind": "a"}, {"kind": "b", "title": "t"}],
                         "the saved specs are the row's own array, and a merge "
                         "that writes into them moves a report nobody touched")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_spec_the_grid_does_not_carry_keeps_its_place(self):
        self.assertEqual(self._merge([{"kind": "a", "x": 0, "y": 0, "w": 4, "h": 4}],
                                     [{"id": "9", "x": 1, "y": 1, "w": 2, "h": 2}])[0],
                         [{"kind": "a", "x": 0, "y": 0, "w": 4, "h": 4}])


class ReportViewerGridTests(SimpleTestCase):

    def test_the_viewer_draws_a_grid_of_cards(self):
        viewer = "".join(_function(CODE, name)
                         for name in ("openReport", "_renderRptGrid", "_rptAddWidget"))
        for needed in ("rpt-gs", "autoPosition", "staticGrid", "data-rpt-layout",
                       "breakpoints", "gridMake("):
            self.assertIn(needed, viewer)

    def test_editing_moves_the_cards_and_done_saves_them(self):
        # One setStatic call flips the grid, so the two directions are read off
        # the two halves of the edit-mode change, not off two literals.
        toggle = _function(CODE, "_rptToggleLayout")
        self.assertIn("setStatic(!on)", _function(CODE, "_rptSetLayoutEditing"))
        self.assertIn("_rptSetLayoutEditing(true)", toggle)
        self.assertIn("_rptSetLayoutEditing(false)", toggle)
        for needed in ("resizestop", ".resize()", "rptMergeLayout("):
            self.assertIn(needed, CODE)

    def test_a_one_column_grid_is_never_saved(self):
        """Below the breakpoint every card is one column wide; saving that
        would squash the real layout."""
        self.assertIn("getColumn() !== 12", _function(CODE, "_rptToggleLayout"))

    def test_charts_are_destroyed_with_their_grid(self):
        body = _function(CODE, "_destroyRptCharts")
        self.assertIn("c.destroy()", body)
        self.assertIn(".destroy(false)", body)

    def test_no_second_grid_stack_item_content(self):
        """Gridstack supplies that wrapper; a second one nests and collapses
        every card to the height of its content."""
        self.assertNotIn("grid-stack-item-content", CODE)

    def test_the_card_body_is_where_the_chart_goes(self):
        body = _function(CODE, "_rptAddWidget")
        self.assertIn('data-rpt-body="${i}"', body)
        self.assertIn("id: String(i)", body)
        self.assertIn("minW: 2", body)
        self.assertIn("minH: 2", body)
        self.assertIn(".csv", body)

    def test_the_old_plain_grid_markup_is_gone(self):
        self.assertNotIn('<div class="rpt-grid">', SRC)

    def test_a_saved_layout_is_sent_as_the_widgets_of_the_open_report(self):
        body = _function(CODE, "_rptSaveLayout")
        self.assertIn("gridLayoutOf(_rptGrid)", body)
        self.assertIn("rptMergeLayout(", body)
        self.assertIn("Layout saved", body)


class ReportTableTests(SimpleTestCase):
    """A widget asked for a table: a days table reads one row per day, and the
    rows widgets keep the record tables they had."""

    def _table(self, widget):
        return _run(f"console.log(JSON.stringify(_rptTableHtml({json.dumps(widget)})));")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_days_table_is_a_day_and_a_column_per_series(self):
        html = self._table(_widget("table", DAYS_WIDGET))
        self.assertIn("<th>Day</th><th>ok</th><th>failed</th><th>pending</th>", html)
        self.assertIn("2026-10-14", html)
        self.assertEqual(html.count("<tr>"), 4)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_blank_day_is_an_empty_cell_not_a_zero(self):
        html = self._table({
            **DAYS_WIDGET, "kind": "success_rate", "chart": "table",
            "data": {"days": ["2026-10-14", "2026-10-15"],
                     "series": [{"name": "Patch web", "values": [None, 80]}]}})
        self.assertIn("<td></td>", html)
        self.assertIn("<td>80%</td>", html)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_a_duration_is_written_out_and_a_count_is_not(self):
        html = self._table({
            **DAYS_WIDGET, "kind": "duration", "chart": "table",
            "data": {"days": ["2026-10-14"], "median": [120], "p95": [None]}})
        self.assertIn("<td>2m</td>", html)
        self.assertIn("<td></td>", html)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_rows_tables_head_their_columns(self):
        step_html = self._table(_widget("table", ROWS_WIDGET))
        branch_html = self._table({
            "kind": "branch_paths", "chart": "table",
            "data": {"rows": [{"source": "Patch web", "branch": "if patchy", "hosts": 2}]}})
        outcome_html = self._table({
            "kind": "host_outcomes", "chart": "table",
            "data": {"rows": [{"host": "web", "source": "Patch web",
                               "outcome": "ok", "at": None}]}})
        self.assertIn("<th>Source</th><th>Step</th><th>Failed</th>", step_html)
        self.assertIn(">if patchy<", branch_html)
        self.assertIn("<th>Source</th><th>Branch</th><th>Host-runs</th>", branch_html)
        self.assertIn(">2<", branch_html)
        self.assertIn("<th>Host</th><th>Source</th><th>Outcome</th><th>When</th>", outcome_html)
        self.assertIn("t-mint", outcome_html)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_an_empty_table_says_so(self):
        self.assertIn("Nothing ran in this range.",
                      self._table({"kind": "host_outcomes", "chart": "table",
                                   "data": {"rows": []}}))


class ReportStyleTests(SimpleTestCase):

    def test_css_appended_not_replaced(self):
        css = CSS.read_text()
        self.assertGreater(len(css.splitlines()), 4575)
        self.assertIn("QA-47: report grid", css)
        self.assertIn(".rpt-gs .rpt-chart", css)
        self.assertRegex(css, r"\.rpt-chart \{ height: 220px; position: relative; \}")

    def test_no_side_stripe(self):
        rules = "\n".join(re.findall(r"\.rpt-[\w-]*\s*\{[^}]*\}", CSS.read_text()))
        for banned in ("border-left", "border-right"):
            self.assertNotIn(banned, rules)
