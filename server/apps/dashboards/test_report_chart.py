"""The ``report_chart`` dashboard widget: the registry entry, the settings
picker it feeds, and the endpoint the card reads.

The card itself is the browser's — it fetches a report's computed payload and
hands the chosen widget to ``_drawRptWidget`` in ``vigil-reports.js``. What is
testable server-side is the contract those two files agree on: that the kind is
registered and free, that the ``<uuid>:<index>`` setting survives the store's
settings hygiene unchanged, and that the data endpoint answers 200 while the
report exists and 404 after it is deleted — which is the only signal the card
has for "This report was removed".
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.playbooks.models import Playbook

from .widgets import WIDGET_REGISTRY, clean_settings

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-widgets.js"


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start : i + 1]
        i += 1


def _node(snippet: str) -> str:
    out = subprocess.run(
        ["node", "-e", snippet], capture_output=True, text=True, timeout=30, check=True
    )
    return out.stdout


class ReportChartRegistryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("rcu", password="pw")
        self.client.force_authenticate(self.user)

    def test_report_chart_is_registered_free_in_the_work_group(self):
        spec = WIDGET_REGISTRY["report_chart"]
        self.assertEqual(spec["group"], "work")
        # Reports are a free feature, so the card must not gate behind one:
        # a "feature" key here would grey the widget out for every free user.
        self.assertNotIn("feature", spec)
        self.assertEqual(spec["settings"]["chart"]["type"], "report_chart")

    def test_chart_setting_is_stored_as_given(self):
        cleaned = clean_settings(
            "report_chart",
            {"chart": "6f6f6f6f-1111-2222-3333-444444444444:2", "junk": 1},
        )
        self.assertEqual(cleaned["chart"], "6f6f6f6f-1111-2222-3333-444444444444:2")
        self.assertNotIn("junk", cleaned)

    def test_the_catalog_offers_report_chart(self):
        body = self.client.get("/api/v1/dashboards/catalog/").json()
        self.assertIn("report_chart", body["widgets"])
        entry = body["widgets"]["report_chart"]
        self.assertEqual(entry["group"], "work")
        self.assertIn("chart", entry["settings"])


class ReportChartScriptTests(SimpleTestCase):
    """The renderer is a text scan plus one run of the pure helper, matching
    how the reports page's own JS is tested."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = JS.read_text()

    def test_the_renderer_is_registered(self):
        self.assertEqual(self.src.count("report_chart: _renderReportChart,"), 1)

    def test_the_renderer_reads_the_report_and_draws_it(self):
        body = _function(self.src, "_renderReportChart")
        for needed in (
            "This report was removed",
            "/api/v1/reports/",
            "/data/",
            "_drawRptWidget",
            "typeof _drawRptWidget",
            "lastIndexOf(':')",
            "_WCHARTS",
        ):
            self.assertIn(needed, body)

    def test_the_settings_picker_offers_report_charts(self):
        options = _function(self.src, "_settingsOptions")
        self.assertIn("'report_chart'", options)
        self.assertIn("/api/v1/reports/", options)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_untitled_report_widgets_are_named_from_their_kind(self):
        """Run the real function from the file. The picker label is the only
        thing a user sees for a widget that has no title of its own."""
        code = "\n".join(
            [
                _function(self.src, "_wReportKindName"),
                (
                    "console.log(JSON.stringify(["
                    "_wReportKindName('success_rate'),"
                    " _wReportKindName('runs_per_day')]));"
                ),
            ]
        )
        self.assertEqual(json.loads(_node(code)), ["Success rate", "Runs per day"])


class ReportChartDataTests(TestCase):
    """The endpoint the card reads, through the same calls the browser makes."""

    def setUp(self):
        self.admin = get_user_model().objects.create_user("rca", password="pw")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.playbook = Playbook.objects.create(
            name="Nightly patch", created_by=self.admin
        )

    def post(self):
        response = self.client.post(
            "/api/v1/reports/",
            {
                "name": "Nightly",
                "description": "how patching goes",
                "range_days": 30,
                "widgets": [
                    {"kind": "success_rate"},
                    {"kind": "runs_per_day", "title": "Volume"},
                ],
                "playbooks": [str(self.playbook.id)],
                "automations": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["id"]

    def test_a_report_answers_with_one_computed_widget_per_saved_one(self):
        report_id = self.post()
        response = self.client.get(f"/api/v1/reports/{report_id}/data/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["widgets"]), 2)

    def test_a_deleted_report_is_a_404_which_is_the_cards_removed_path(self):
        report_id = self.post()
        self.assertEqual(
            self.client.get(f"/api/v1/reports/{report_id}/data/").status_code, 200
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/reports/{report_id}/").status_code, 204
        )
        self.assertEqual(
            self.client.get(f"/api/v1/reports/{report_id}/data/").status_code, 404
        )
