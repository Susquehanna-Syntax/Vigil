"""Widget specs (QA-46): each widget's own chart, labels, sources and place.

``tests.py`` covers the report itself. These tests cover what a widget now
carries with it: the chart that suits its metric, the axis labels and legend,
the subset of the report's sources it draws, the grid place its card holds, and
the preview that draws all of it before anything is saved.
"""

from apps.playbooks.models import Playbook
from apps.reports import views, widgets
from apps.reports.models import Report
from apps.reports.tests import NOW, TODAY, ReportTestBase

BAD_PLACE = "a widget's place must fit a 12-column grid"


class CatalogTests(ReportTestBase):
    def test_catalog_offers_charts_labels_and_how(self):
        catalog = self.client.get("/api/v1/reports/widgets/").json()
        self.assertEqual([w["kind"] for w in catalog], list(widgets.WIDGETS))
        for entry in catalog:
            for key in ("family", "x_label", "y_label", "how", "size"):
                self.assertIn(key, entry, entry["kind"])
            self.assertTrue(entry["charts"], entry["kind"])
            self.assertEqual(entry["charts"][0], entry["chart"], entry["kind"])
            self.assertTrue(entry["how"], entry["kind"])
            self.assertEqual(entry["size"], {"w": 4, "h": 4}, entry["kind"])

        by_kind = {w["kind"]: w for w in catalog}
        self.assertEqual(by_kind["success_rate"]["charts"],
                         ["line", "area", "bar", "table"])
        self.assertEqual(by_kind["host_outcomes"]["charts"][0], "table")


class WidgetSpecTests(ReportTestBase):
    def saved(self, pb, specs, name="Weekly"):
        """A report holding these exact widget specs, over *pb* (a playbook or a
        list of them)."""
        report = self.report(sources=[pb] if isinstance(pb, Playbook) else pb, name=name)
        report.widgets = specs
        report.save()
        return report

    def test_old_widgets_compute_as_before(self):
        pb = self.playbook("Patch web")
        report = self.saved(pb, [{"kind": "success_rate"}])
        widget = self.data(report)["widgets"][0]
        self.assertEqual(widget["chart"], "line")
        self.assertEqual(widget["x_label"], "Day")
        self.assertEqual(widget["y_label"], "Succeeded (%)")
        self.assertIsNone(widget["legend"])
        self.assertIsNone(widget["layout"])

    def test_chosen_chart_and_labels_round_trip(self):
        pb = self.playbook("Patch web")
        widget = {"kind": "runs_per_day", "chart": "line", "x_label": "",
                  "y_label": "Hosts", "legend": False, "x": 4, "y": 0, "w": 8, "h": 5}
        created = self.client.post("/api/v1/reports/", {
            "name": "Nightly", "range_days": 7, "widgets": [widget],
            "playbooks": [str(pb.id)], "automations": []}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["widgets"], [widget])

        report_id = created.json()["id"]
        data = self.client.get(f"/api/v1/reports/{report_id}/data/").json()
        computed = data["widgets"][0]
        self.assertEqual(computed["chart"], "line")
        self.assertEqual(computed["x_label"], "")
        self.assertEqual(computed["y_label"], "Hosts")
        self.assertIs(computed["legend"], False)
        self.assertEqual(computed["layout"], {"x": 4, "y": 0, "w": 8, "h": 5})

    def test_each_bad_widget_is_refused(self):
        pb = self.playbook("Patch web")
        place = {"x": 0, "y": 0, "w": 4, "h": 4}
        for widget, detail in (
                ({"kind": "success_rate", "chart": "pie"},
                 "chart 'pie' does not suit success_rate"),
                ({"kind": "success_rate", "x_label": "x" * 41},
                 "axis labels must be text of at most 40 characters"),
                ({"kind": "success_rate", "legend": "yes"},
                 "legend must be true, false or null"),
                ({"kind": "success_rate", "sources": ["not-a-uuid"]},
                 "sources must be a list of playbook or automation ids"),
                ({"kind": "success_rate", "x": 0, "y": 0, "h": 4},
                 "a widget's x, y, w and h go together"),
                ({"kind": "success_rate", "x": 10, "y": 0, "w": 4, "h": 4},
                 BAD_PLACE),
                ({"kind": "success_rate", "x": 0, "y": 0, "w": 4, "h": 1},
                 BAD_PLACE),
                ({"kind": "success_rate", "x": True, "y": 0, "w": 4, "h": 4},
                 BAD_PLACE)):
            response = self.client.post("/api/v1/reports/", {
                "name": "Nightly", "range_days": 7, "widgets": [widget],
                "playbooks": [str(pb.id)], "automations": []}, format="json")
            self.assertEqual(response.status_code, 400, widget)
            self.assertEqual(response.json()["detail"], detail, widget)
        self.assertEqual(place, {"x": 0, "y": 0, "w": 4, "h": 4})

    def test_the_same_metric_twice(self):
        pb = self.playbook("Patch web")
        self.host_run(playbook=pb, day=TODAY, steps=(("patch", "completed"),))
        report = self.saved(pb, [
            {"kind": "success_rate", "chart": "line"},
            {"kind": "success_rate", "chart": "table", "title": "As numbers"}])
        computed = self.data(report)["widgets"]
        self.assertEqual([w["chart"] for w in computed], ["line", "table"])
        self.assertEqual([w["title"] for w in computed],
                         ["Success rate", "As numbers"])
        self.assertEqual(computed[1]["data"]["series"], computed[0]["data"]["series"])

        csv = self.csv(report, 1)
        self.assertEqual(csv.status_code, 200, csv.content)
        self.assertEqual(csv.content.decode().splitlines()[0], "day,Patch web")

    def test_a_widget_can_narrow_its_sources(self):
        first, second = self.playbook("Patch web"), self.playbook("Patch db")
        self.host_run(playbook=first, day=TODAY, host="web",
                      steps=(("patch", "completed"),))
        self.host_run(playbook=second, day=TODAY, host="db",
                      steps=(("patch", "failed"),))
        outside = str(self.playbook("Patch edge").id)
        report = self.saved(
            [first, second],
            [{"kind": "success_rate", "sources": [str(first.id)]},
             {"kind": "runs_per_day", "sources": [str(first.id)]},
             {"kind": "success_rate"},
             {"kind": "success_rate", "sources": [outside]},
             {"kind": "runs_per_day", "sources": [outside]}])
        rate_one, volume_one, rate_both, rate_none, volume_none = (
            w["data"] for w in self.data(report)["widgets"])

        self.assertEqual([s["name"] for s in rate_one["series"]], ["Patch web"])
        self.assertEqual(sum(volume_one["ok"]) + sum(volume_one["failed"]), 1)
        self.assertEqual({s["name"] for s in rate_both["series"]},
                         {"Patch web", "Patch db"})
        self.assertEqual([s["name"] for s in rate_none["series"]], [])
        self.assertEqual(volume_none["ok"], [0] * len(volume_none["days"]))
        self.assertEqual(volume_none["failed"], [0] * len(volume_none["days"]))

    def _hidden(self, *sources):
        """Make *sources* invisible: no view of them can select or draw them."""
        pks = [source.pk for source in sources]
        for module in (views, widgets):
            real = module.scoping.filter_by_site

            def hide(qs, user, path="", cascade_global=False, _real=real):
                return _real(qs, user, path=path,
                             cascade_global=cascade_global).exclude(pk__in=pks)

            self.addsetattr(module.scoping, "filter_by_site", hide)

    def preview(self, client, body):
        """POST a preview with the clock held where the fixture rows are."""
        original = widgets.datetime

        class Frozen(original):  # type: ignore[misc]
            @classmethod
            def now(cls, tz=None):
                return NOW

        self.addsetattr(widgets, "datetime", Frozen)
        return client.post("/api/v1/reports/preview/", body, format="json")

    def test_preview_draws_without_saving(self):
        mine = self.playbook("Patch web")
        other_site = self.playbook("Patch db")
        self.host_run(playbook=mine, day=TODAY, steps=(("patch", "completed"),))
        self._hidden(other_site)
        before = Report.objects.count()
        body = {"range_days": 7, "widgets": [{"kind": "success_rate"}],
                "playbooks": [str(mine.id)], "automations": []}

        preview = self.preview(self.client, body)
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual([w["kind"] for w in preview.json()["widgets"]],
                         ["success_rate"])
        self.assertEqual(preview.json()["widgets"][0]["data"]["series"][0]["values"][-1],
                         100.0)
        self.assertEqual(Report.objects.count(), before)

        missing = self.preview(self.client, {**body, "range_days": None})
        self.assertEqual(missing.status_code, 400, missing.content)

        # A viewer is allowed to preview — but only from what they can see.
        self.assertEqual(self.preview(self.as_viewer(), body).status_code, 200)
        self.assertEqual(Report.objects.count(), before)
        hidden = self.preview(self.as_viewer(),
                              {**body, "playbooks": [str(other_site.id)]})
        self.assertEqual(hidden.status_code, 400, hidden.content)


class ReviewFixTests(ReportTestBase):
    def test_a_preview_missing_its_range_or_widgets_is_a_400(self):
        pb = self.playbook("Patch web")
        body = {"range_days": 7, "widgets": [{"kind": "success_rate"}],
                "playbooks": [str(pb.id)], "automations": []}
        for key in ("range_days", "widgets"):
            short = {k: v for k, v in body.items() if k != key}
            response = self.client.post("/api/v1/reports/preview/", short, format="json")
            self.assertEqual(response.status_code, 400, f"{key}: {response.content}")

    def test_column_labels_turn_round_with_the_chart(self):
        self.assertEqual(widgets.default_labels("failures_by_step", "bar"), ("Failures", "Step"))
        self.assertEqual(widgets.default_labels("failures_by_step", "column"), ("Step", "Failures"))
        self.assertEqual(widgets.default_labels("host_outcomes", "column"), ("Outcome", "Hosts"))
        rows = {r["kind"]: r for r in self.client.get("/api/v1/reports/widgets/").json()}
        self.assertEqual(rows["branch_paths"]["labels"],
                         {"bar": ["Host-runs", "Branch"], "column": ["Branch", "Host-runs"]})
        self.assertEqual(rows["success_rate"]["labels"]["line"], ["Day", "Succeeded (%)"])
        self.assertNotIn("table", rows["success_rate"]["labels"])
        pb = self.playbook("Patch web")
        report = self.client.post("/api/v1/reports/", {
            "name": "Cols", "range_days": 7, "playbooks": [str(pb.id)],
            "widgets": [{"kind": "failures_by_step", "chart": "column"}]}, format="json").json()
        data = self.client.get(f"/api/v1/reports/{report['id']}/data/").json()
        self.assertEqual((data["widgets"][0]["x_label"], data["widgets"][0]["y_label"]),
                         ("Step", "Failures"))
