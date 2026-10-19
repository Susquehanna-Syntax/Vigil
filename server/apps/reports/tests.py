"""Saved reports: the model, the API and the widget computations (QA-41).

Everything here is server-side: the page that draws the charts is QA-42. The
behaviour that matters is that every number comes from run history through
``apps.tasks.summary._host_state``, so a report says what the run detail page
says — and that a report never shows a source the viewer cannot open.
"""

from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.automations.models import Automation
from apps.hosts.models import Host
from apps.playbooks.models import Playbook
from apps.tasks.models import Task, TaskRun
from vigil import scoping

from . import widgets
from .models import Report

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-10-15"
YESTERDAY = "2026-10-14"


class ReportTestBase(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("ra", password="pw")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.viewer = get_user_model().objects.create_user("rv", password="pw")
        UserProfile.objects.create(user=self.viewer, role=Role.VIEWER)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.hosts = {}
        self.names = set()
        self.counter = 0

    def addsetattr(self, obj, name, value):
        """Swap a module attribute for the duration of the test."""
        original = getattr(obj, name)
        self.addCleanup(setattr, obj, name, original)
        setattr(obj, name, value)

    def as_viewer(self):
        self.client.force_authenticate(self.viewer)
        return self.client

    def host(self, hostname):
        self.counter += 1
        host = Host.objects.create(hostname=hostname, agent_token=f"tok-r{self.counter}",
                                   status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        self.hosts[hostname] = host
        return host

    def playbook(self, name):
        # Distinct source names keep a widget's series unambiguous.
        self._use_name(name)
        return Playbook.objects.create(name=name, created_by=self.admin)

    def automation(self, name, playbook=None):
        self._use_name(name)
        return Automation.objects.create(name=name, trigger="event", event="alert_sent",
                                         action_kind="playbook", playbook=playbook,
                                         target="all", created_by=self.admin)

    def _use_name(self, name):
        self.assertNotIn(name, self.names, f"test reused source name {name!r}")
        self.names.add(name)

    def host_run(self, *, day=TODAY, playbook=None, automation=None, host="web",
                 steps=(("patch", "completed"),), branch="", goto=False, seconds=60):
        """One host-run: a TaskRun dated *day* with one task per (step, state).

        ``seconds`` is the wall-clock length, measured from each task's
        dispatch to its completion, so a host-run's duration is exactly what
        the duration widget reads. ``state="continue"`` is a failure the
        playbook handles: a ``failed`` row carrying ``on_failure: continue``.
        """
        run = TaskRun.objects.create(playbook=playbook, automation=automation,
                                     name_snapshot=automation.name if automation
                                     else (playbook.name if playbook else "run"))
        self._backdate_run(run, day)
        host_row = self.hosts.get(host) or self.host(host)
        finished = day_start(day) + timedelta(hours=9)
        for order, (step, state) in enumerate(steps):
            failed = state in ("failed", "continue")
            done = None
            dispatched = finished - timedelta(seconds=seconds)
            if state not in ("pending", "dispatched", "blocked"):
                done = finished
            task = Task.objects.create(
                run=run, host=host_row, action="_goto" if goto else "_script",
                state="failed" if failed else state, step_ref=step, branch=branch,
                step_order=order,
                on_failure="continue" if state == "continue" else "",
                dispatched_at=dispatched, completed_at=done, nonce=f"n-{self._seq()}")
            self._backdate_task(task, day)
        return run

    def _seq(self):
        self.counter += 1
        return self.counter

    def _backdate_run(self, run, day):
        when = day_start(day) + timedelta(hours=8)
        TaskRun.objects.filter(pk=run.pk).update(created_at=when)
        run.created_at = when

    def _backdate_task(self, task, day):
        when = day_start(day) + timedelta(hours=9)
        Task.objects.filter(pk=task.pk).update(created_at=when)

    def report(self, sources=(), range_days=7, widget_kinds=("success_rate",),
               **kwargs):
        report = Report.objects.create(name=kwargs.pop("name", "Weekly"),
                                       owner=self.admin, range_days=range_days,
                                       widgets=[{"kind": k} for k in widget_kinds],
                                       **kwargs)
        for source in sources:
            if isinstance(source, Playbook):
                report.playbooks.add(source)
            else:
                report.automations.add(source)
        return report

    def csv(self, report, index, now=NOW):
        """GET a widget's CSV with the clock held at *now*, so the day window
        the endpoint computes is the one the fixture rows were written for."""
        original = widgets.datetime

        class Frozen(original):  # type: ignore[misc]
            @classmethod
            def now(cls, tz=None):
                return now

        self.addsetattr(widgets, "datetime", Frozen)
        return self.client.get(f"/api/v1/reports/{report.id}/data/{index}.csv")

    def data(self, report, kind=None, user=None, now=NOW):
        computed = widgets.compute(report, user or self.admin, now=now)
        if kind is None:
            return computed
        for widget in computed["widgets"]:
            if widget["kind"] == kind:
                return widget["data"]
        raise AssertionError(f"{kind} not computed")


def day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


class CrudTests(ReportTestBase):
    def test_crud_and_validation(self):
        pb = self.playbook("Patch web")
        created = self.client.post("/api/v1/reports/", {
            "name": "Nightly", "description": "how patching goes", "range_days": 30,
            "widgets": [{"kind": "success_rate"}, {"kind": "runs_per_day", "title": "Volume"}],
            "playbooks": [str(pb.id)], "automations": []}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual([w["kind"] for w in created.json()["widgets"]],
                         ["success_rate", "runs_per_day"])
        self.assertEqual(created.json()["playbooks"], [str(pb.id)])
        url = f"/api/v1/reports/{created.json()['id']}/"

        patched = self.client.patch(url, {"range_days": 7,
                                          "widgets": [{"kind": "host_outcomes"}]},
                                    format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(patched.json()["range_days"], 7)
        self.assertEqual(patched.json()["name"], "Nightly")
        self.assertEqual([w["kind"] for w in patched.json()["widgets"]], ["host_outcomes"])

        self.as_viewer()
        forbidden = self.client.post("/api/v1/reports/", {
            "name": "Nope", "range_days": 30, "widgets": [{"kind": "success_rate"}],
            "playbooks": [str(pb.id)]}, format="json")
        self.assertEqual(forbidden.status_code, 403, forbidden.content)
        listed = self.client.get("/api/v1/reports/")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([r["name"] for r in listed.json()], ["Nightly"])

    def test_rejects_each_malformed_payload(self):
        pb = self.playbook("Patch web")
        base = {"name": "Nightly", "range_days": 30,
                "widgets": [{"kind": "success_rate"}], "playbooks": [str(pb.id)]}

        def refused(change):
            payload = {**base, **change}
            response = self.client.post("/api/v1/reports/", payload, format="json")
            self.assertEqual(response.status_code, 400, f"{change} -> {response.content}")
            self.assertIn("detail", response.json())
            self.assertEqual(response.json()["detail"], str(response.json()["detail"]))

        refused({"widgets": []})
        refused({"widgets": [{"kind": "sparklines"}]})
        refused({"range_days": 10})
        refused({"playbooks": [], "automations": []})
        refused({"playbooks": [str(self.playbook("Other").id),
                               "11111111-1111-1111-1111-111111111111"]})
        refused({"name": ""})
        refused({"name": "x" * 121})
        refused({"widgets": [{"kind": "success_rate", "colour": "red"}]})
        refused({"widgets": [{"kind": "success_rate", "title": "t" * 81}]})
        refused({"widgets": [{"kind": k} for k in
                             ("success_rate", "runs_per_day", "failures_by_step",
                              "branch_paths", "duration", "host_outcomes",
                              "automation_fires", "success_rate", "runs_per_day",
                              "failures_by_step", "branch_paths", "duration",
                              "host_outcomes", "automation_fires", "success_rate")]})
        self.assertEqual(Report.objects.count(), 0)

    def test_empty_source_lists_are_a_400(self):
        base = {"name": "Nightly", "range_days": 30,
                "widgets": [{"kind": "success_rate"}]}
        response = self.client.post("/api/v1/reports/",
                                    {**base, "playbooks": [], "automations": []},
                                    format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Report.objects.count(), 0)

    def test_partial_update_keeps_its_sources(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb])
        response = self.client.patch(f"/api/v1/reports/{report.id}/",
                                     {"name": "Renamed"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["playbooks"], [str(pb.id)])
        emptied = self.client.patch(f"/api/v1/reports/{report.id}/",
                                    {"playbooks": []}, format="json")
        self.assertEqual(emptied.status_code, 400, emptied.content)

    def test_detail_delete_and_catalog(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb])
        url = f"/api/v1/reports/{report.id}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(Report.objects.count(), 0)
        self.assertEqual(self.client.get(url).status_code, 404)


class CatalogTests(ReportTestBase):
    def test_widget_catalog(self):
        response = self.client.get("/api/v1/reports/widgets/")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertEqual([r["kind"] for r in rows],
                         ["success_rate", "runs_per_day", "failures_by_step",
                          "branch_paths", "duration", "host_outcomes",
                          "automation_fires"])
        self.assertEqual([r["chart"] for r in rows],
                         ["line", "stacked-bar", "bar", "bar", "line", "table",
                          "stacked-bar"])
        for row in rows:
            self.assertTrue(row["title"] and row["description"])


class DayRangeTests(ReportTestBase):
    def test_days_cover_the_range(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7)
        computed = widgets.compute(report, self.admin, now=NOW)
        self.assertEqual(computed["range"]["from"], "2026-10-09")
        self.assertEqual(computed["range"]["to"], TODAY)
        self.assertEqual(computed["range"]["days"],
                         ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12",
                          "2026-10-13", "2026-10-14", TODAY])
        self.assertFalse(computed["truncated"])
        rate = self.data(report, "success_rate")
        self.assertEqual(rate["days"], computed["range"]["days"])
        # The selected playbook gets a series even with no runs in the window.
        self.assertEqual(rate["series"], [{"name": "Patch web",
                                          "values": [None] * 7}])


class OutcomeTests(ReportTestBase):
    def setUp(self):
        super().setUp()
        self.first = self.playbook("Patch web")
        self.second = self.playbook("Harden db")
        self.report_row = self.report(
            sources=[self.first, self.second], range_days=7,
            widget_kinds=("success_rate", "runs_per_day"))

        # Day 1: web ok, db failed outright, db recoverable failure.
        self.host_run(playbook=self.first, day="2026-10-13", host="web",
                      steps=(("patch", "completed"),))
        self.host_run(playbook=self.second, day="2026-10-13", host="db",
                      steps=(("harden", "failed"),))
        self.host_run(playbook=self.second, day="2026-10-13", host="db2",
                      steps=(("harden", "continue"),))
        # Day 2: web ok, web2 still running; db ok, db2 ok after a failure the
        # playbook handles (on_failure: continue), db3 failed outright.
        self.host_run(playbook=self.first, day="2026-10-14", host="web",
                      steps=(("patch", "completed"),))
        self.host_run(playbook=self.first, day="2026-10-14", host="web2",
                      steps=(("patch", "dispatched"),))
        self.host_run(playbook=self.second, day="2026-10-14", host="db",
                      steps=(("harden", "completed"),))
        self.host_run(playbook=self.second, day="2026-10-14", host="db2",
                      steps=(("rescan", "continue"), ("harden", "completed")))
        self.host_run(playbook=self.second, day="2026-10-14", host="db3",
                      steps=(("harden", "failed"),))

    def test_success_rate_and_runs_per_day(self):
        rate = self.data(self.report_row, "success_rate")
        by_name = {s["name"]: s["values"] for s in rate["series"]}
        self.assertEqual(sorted(by_name), ["Harden db", "Patch web"])
        self.assertEqual(by_name["Patch web"][-3:], [100.0, 100.0, None])
        self.assertEqual(by_name["Harden db"][-3:], [50.0, 66.7, None])

        counts = self.data(self.report_row, "runs_per_day")
        self.assertEqual(counts["ok"][-3:], [2, 3, 0])
        self.assertEqual(counts["failed"][-3:], [1, 1, 0])
        self.assertEqual(counts["pending"][-3:], [0, 1, 0])

    def test_a_pending_host_run_does_not_dilute_the_rate(self):
        """The denominator is finished host-runs only: Harden db's three on
        10-14 are its rate, the one host still running is not in it."""
        rate = self.data(self.report_row, "success_rate")
        self.assertEqual({s["name"]: s["values"][-2] for s in rate["series"]},
                         {"Patch web": 100.0, "Harden db": 66.7})
        counts = self.data(self.report_row, "runs_per_day")
        self.assertEqual(counts["pending"][-2], 1)


class FailureTests(ReportTestBase):
    def test_failures_by_step_excludes_goto_and_handled_failures(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("failures_by_step",))
        self.host_run(playbook=pb, day=TODAY, host="web", goto=True,
                      steps=(("@go", "failed"),))
        self.host_run(playbook=pb, day=TODAY, host="db",
                      steps=(("recover", "continue"),))
        self.host_run(playbook=pb, day=TODAY, host="app",
                      steps=(("patch", "failed"),))
        rows = self.data(report, "failures_by_step")["rows"]
        self.assertEqual(rows, [{"source": "Patch web", "step": "patch", "failed": 1}])

    def test_directive_task_without_a_step_ref_groups_as_task(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("failures_by_step",))
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("", "failed"),))
        rows = self.data(report, "failures_by_step")["rows"]
        self.assertEqual(rows, [{"source": "Patch web", "step": "(task)", "failed": 1}])


class BranchTests(ReportTestBase):
    def test_branch_paths_count_distinct_hosts(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7, widget_kinds=("branch_paths",))
        self.host_run(playbook=pb, day=TODAY, host="web", branch="b1.then",
                      steps=(("a", "completed"), ("b", "completed")))
        self.host_run(playbook=pb, day=TODAY, host="db", branch="b1.then",
                      steps=(("a", "completed"),))
        self.host_run(playbook=pb, day=TODAY, host="app", branch="b1.else",
                      steps=(("c", "completed"),))
        self.host_run(playbook=pb, day=TODAY, host="skip", branch="b1.else",
                      steps=(("c", "skipped"),))
        rows = self.data(report, "branch_paths")["rows"]
        self.assertEqual(rows, [
            {"source": "Patch web", "branch": "b1.else", "hosts": 1},
            {"source": "Patch web", "branch": "b1.then", "hosts": 2},
        ])

    def test_a_host_taking_a_branch_in_two_runs_counts_twice(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7, widget_kinds=("branch_paths",))
        self.host_run(playbook=pb, day=YESTERDAY, host="web", branch="b1.then")
        self.host_run(playbook=pb, day=TODAY, host="web", branch="b1.then")
        rows = self.data(report, "branch_paths")["rows"]
        self.assertEqual(rows, [{"source": "Patch web", "branch": "b1.then", "hosts": 2}])


class SharedHostnameTests(ReportTestBase):
    def test_two_hosts_sharing_a_hostname_stay_two_host_runs(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("runs_per_day", "host_outcomes"))
        run = self.host_run(playbook=pb, day=TODAY, host="web",
                            steps=(("patch", "completed"),))
        twin = Host.objects.create(hostname="web", agent_token="tok-twin",
                                   status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        task = Task.objects.create(run=run, host=twin, action="_script", state="failed",
                                   step_ref="patch", nonce="n-twin")
        self._backdate_task(task, TODAY)
        computed = widgets.compute(report, self.admin, now=NOW)["widgets"]
        per_day, outcomes = computed[0]["data"], computed[1]["data"]
        self.assertEqual((per_day["ok"][-1], per_day["failed"][-1]), (1, 1))
        self.assertEqual(sorted(r["outcome"] for r in outcomes["rows"]), ["failed", "ok"])


class DurationTests(ReportTestBase):
    def test_duration_percentiles(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7, widget_kinds=("duration",))
        for seconds, host in ((10, "h1"), (20, "h2"), (30, "h3"), (40, "h4"),
                              (100, "h5")):
            self.host_run(playbook=pb, day=TODAY, host=host,
                          steps=(("patch", "completed"),), seconds=seconds)
        data = self.data(report, "duration")
        self.assertEqual(data["median"][-1], 30)
        self.assertEqual(data["p95"][-1], 100)
        self.assertEqual(data["median"][-2], None)
        self.assertEqual(data["p95"][-2], None)


class HostOutcomeTests(ReportTestBase):
    def test_host_outcomes_newest_wins(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7, widget_kinds=("host_outcomes",))
        self.host_run(playbook=pb, day=YESTERDAY, host="web", steps=(("patch", "failed"),))
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("patch", "completed"),))
        self.host_run(playbook=pb, day=YESTERDAY, host="db", steps=(("patch", "failed"),))
        rows = self.data(report, "host_outcomes")["rows"]
        self.assertEqual([(r["host"], r["outcome"]) for r in rows],
                         [("db", "failed"), ("web", "ok")])
        self.assertEqual(rows[1]["at"][:10], TODAY)
        self.assertEqual(rows[0]["source"], "Patch web")


class AttributionTests(ReportTestBase):
    def test_titles_and_the_task_row_cap(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb])
        report.widgets = [{"kind": "success_rate"},
                          {"kind": "runs_per_day", "title": "Volume, daily"}]
        report.save()
        computed = widgets.compute(report, self.admin, now=NOW)
        self.assertEqual([w["title"] for w in computed["widgets"]],
                         ["Success rate", "Volume, daily"])
        self.assertEqual([w["chart"] for w in computed["widgets"]],
                         ["line", "stacked-bar"])
        self.assertFalse(computed["truncated"])

        # One row of room: the newest task wins and the page is told to say so.
        self.host_run(playbook=pb, day=TODAY, host="web",
                      steps=(("patch", "completed"), ("reboot", "completed")))
        self.addsetattr(widgets, "_MAX_TASK_ROWS", 1)
        capped = widgets.compute(report, self.admin, now=NOW)
        self.assertTrue(capped["truncated"])
        self.assertEqual(capped["widgets"][1]["data"]["ok"][-1], 1)

    def test_automation_run_is_attributed_once(self):
        pb = self.playbook("Patch web")
        auto = self.automation("Nightly patch", playbook=pb)
        report = self.report(sources=[pb, auto], range_days=7,
                             widget_kinds=("success_rate", "runs_per_day",
                                           "automation_fires"))
        self.host_run(playbook=pb, automation=auto, day=TODAY, host="web",
                      steps=(("patch", "completed"),))
        rate = self.data(report, "success_rate")
        # The run is the automation's: the playbook it also names has no hits.
        self.assertEqual([s["name"] for s in rate["series"]],
                         ["Nightly patch", "Patch web"])
        self.assertEqual(rate["series"][0]["values"][-1], 100.0)
        self.assertEqual(rate["series"][1]["values"], [None] * 7)
        counts = self.data(report, "runs_per_day")
        self.assertEqual(counts["ok"][-1], 1)
        fires = self.data(report, "automation_fires")
        self.assertEqual([s["name"] for s in fires["series"]], ["Nightly patch"])
        self.assertEqual(fires["series"][0]["values"][-1], 1)

    def test_automation_fires_counts_only_selected_automations(self):
        auto = self.automation("Nightly patch")
        other = self.automation("Reboot db")
        report = self.report(sources=[auto], range_days=7,
                             widget_kinds=("automation_fires",))
        self.host_run(automation=auto, day=TODAY, host="web",
                      steps=(("_script", "completed"),))
        self.host_run(automation=other, day=TODAY, host="db",
                      steps=(("_script", "completed"),))
        fires = self.data(report, "automation_fires")
        self.assertEqual([s["name"] for s in fires["series"]], ["Nightly patch"])
        self.assertEqual([s["values"][-1] for s in fires["series"]], [1])


class ScopingTests(ReportTestBase):
    def test_scoping_hides_invisible_sources(self):
        pb = self.playbook("Patch web")
        auto = self.automation("Nightly patch", playbook=pb)
        report = self.report(sources=[pb, auto], range_days=7,
                             widget_kinds=("success_rate", "automation_fires"))
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("patch", "completed"),))
        self.host_run(playbook=pb, automation=auto, day=TODAY, host="db",
                      steps=(("patch", "completed"),))

        real = widgets.scoping.filter_by_site

        def only_playbooks(qs, user, path="", cascade_global=False):
            if qs.model is Automation:
                return qs.none()
            return real(qs, user, path=path, cascade_global=cascade_global)

        self.addsetattr(widgets.scoping, "filter_by_site", only_playbooks)
        data = widgets.compute(report, self.viewer, now=NOW)
        rate = data["widgets"][0]["data"]
        self.assertEqual([s["name"] for s in rate["series"]], ["Patch web"])
        self.assertEqual(data["widgets"][1]["data"]["series"], [])

    def test_hosts_the_viewer_cannot_see_drop_out(self):
        pb = self.playbook("Patch web")
        auto = self.automation("Nightly patch", playbook=pb)
        report = self.report(sources=[pb, auto], range_days=7,
                             widget_kinds=("runs_per_day", "host_outcomes", "automation_fires"))
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("patch", "completed"),))
        self.host_run(playbook=pb, automation=auto, day=TODAY, host="hidden",
                      steps=(("patch", "failed"),))
        hidden = self.hosts["hidden"]
        real = widgets.scoping.filter_by_site

        def hide_one_host(qs, user, path="", cascade_global=False):
            if qs.model is Task:
                return qs.exclude(host=hidden)
            return real(qs, user, path=path, cascade_global=cascade_global)

        self.addsetattr(widgets.scoping, "filter_by_site", hide_one_host)
        per_day, outcomes, fires = (w["data"] for w in
                                    widgets.compute(report, self.viewer, now=NOW)["widgets"])
        self.assertEqual((per_day["ok"][-1], per_day["failed"][-1]), (1, 0))
        self.assertEqual([r["host"] for r in outcomes["rows"]], ["web"])
        self.assertEqual(fires["series"], [{"name": "Nightly patch", "values": [0] * 7}])

    def test_invisible_source_cannot_be_selected(self):
        pb = self.playbook("Patch web")

        def nothing(qs, user, path="", cascade_global=False):
            return qs.none()

        self.addsetattr(scoping, "filter_by_site", nothing)
        response = self.client.post("/api/v1/reports/", {
            "name": "Nightly", "range_days": 30,
            "widgets": [{"kind": "success_rate"}], "playbooks": [str(pb.id)]},
            format="json")
        self.assertEqual(response.status_code, 400, response.content)


class ReportAccessTests(ReportTestBase):
    """A site-scoped user sees only reports with a source they can open, and
    an edit never drops the sources they were not shown."""

    def _scope_away(self, hidden):
        from apps.reports import views

        real = views.scoping.filter_by_site

        def hide(qs, user, path="", cascade_global=False):
            return real(qs, user, path=path, cascade_global=cascade_global).exclude(
                pk__in=[h.pk for h in hidden if h._meta.model is qs.model])

        self.addsetattr(views.scoping, "filter_by_site", hide)
        self.addsetattr(__import__("apps.accounts.permissions", fromlist=["x"]),
                        "visible_site_ids", lambda user: [1])

    def test_another_sites_report_is_a_404(self):
        theirs = self.playbook("Their patch")
        report = self.report(sources=[theirs], name="Theirs")
        self._scope_away([theirs])
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get("/api/v1/reports/").json(), [])
        for url in (f"/api/v1/reports/{report.id}/", f"/api/v1/reports/{report.id}/data/",
                    f"/api/v1/reports/{report.id}/data/0.csv"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        # An operator of another site cannot delete it either; its owner can.
        other = get_user_model().objects.create_user("rop", password="pw")
        UserProfile.objects.create(user=other, role=Role.OPERATOR)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.delete(f"/api/v1/reports/{report.id}/").status_code, 404)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"/api/v1/reports/{report.id}/").status_code, 200)

    def test_an_edit_keeps_the_sources_it_was_not_shown(self):
        mine, theirs = self.playbook("My patch"), self.playbook("Their patch")
        report = self.report(sources=[mine, theirs])
        self._scope_away([theirs])
        row = self.client.get(f"/api/v1/reports/{report.id}/").json()
        self.assertEqual(row["playbooks"], [str(mine.id)])
        response = self.client.patch(f"/api/v1/reports/{report.id}/",
                                     {"playbooks": [str(mine.id)]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(report.playbooks.all()), {mine, theirs})


class CsvTests(ReportTestBase):
    def test_csv_export(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("success_rate", "host_outcomes"))
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("patch", "completed"),))

        response = self.csv(report, 0)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="weekly-success_rate.csv"')
        lines = response.content.decode().strip().splitlines()
        self.assertEqual(lines[0], "day,Patch web")
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[-1], f"{TODAY},100.0")
        self.assertEqual(lines[-2], f"{YESTERDAY},")

        rows = self.csv(report, 1)
        self.assertEqual(rows.status_code, 200, rows.content)
        table = rows.content.decode().strip().splitlines()
        self.assertEqual(table[0], "at,host,outcome,source")
        self.assertEqual(len(table), 2)

        missing = self.csv(report, 99)
        self.assertEqual(missing.status_code, 404)

    def test_csv_of_a_rows_widget_with_no_rows_keeps_its_header(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], widget_kinds=("failures_by_step",))
        table = self.csv(report, 0).content.decode().strip().splitlines()
        self.assertEqual(table, ["failed,source,step"])

    def test_csv_of_the_duration_widget(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], widget_kinds=("duration",))
        self.host_run(playbook=pb, day=TODAY, host="web",
                      steps=(("patch", "completed"),), seconds=45)
        table = self.csv(report, 0).content.decode().strip().splitlines()
        self.assertEqual(table[0], "day,median,p95")
        self.assertEqual(table[-1], f"{TODAY},45,45")
        self.assertEqual(table[-2], f"{YESTERDAY},,")

    def test_csv_quotes_a_source_needing_it(self):
        pb = self.playbook("Patch, web")
        report = self.report(sources=[pb], widget_kinds=("success_rate",))
        self.host_run(playbook=pb, day=TODAY, host="web",
                      steps=(("patch", "completed"),))
        lines = self.csv(report, 0).content.decode().strip().splitlines()
        self.assertEqual(lines[0], 'day,"Patch, web"')
        self.assertEqual(lines[-1], f"{TODAY},100.0")

    def test_every_route_through_the_mounted_urlconf(self):
        """The URLs this phase installs, exercised at their real paths — the
        catalog must win against the report-id converter, and the CSV suffix
        must resolve inside the report prefix."""
        pb = self.playbook("Patch web")
        created = self.client.post("/api/v1/reports/", {
            "name": "Weekly", "range_days": 7,
            "widgets": [{"kind": "success_rate"}, {"kind": "host_outcomes"}],
            "playbooks": [str(pb.id)]}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        report_id = created.json()["id"]

        catalog = self.client.get("/api/v1/reports/widgets/")
        self.assertEqual(catalog.status_code, 200, catalog.content)
        self.assertEqual(len(catalog.json()), 7)

        listed = self.client.get("/api/v1/reports/")
        self.assertEqual([r["name"] for r in listed.json()], ["Weekly"])

        data = self.client.get(f"/api/v1/reports/{report_id}/data/")
        self.assertEqual(data.status_code, 200, data.content)
        self.assertEqual(len(data.json()["range"]["days"]), 7)
        self.assertEqual(data.json()["range"]["days"][-1],
                         datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self.assertEqual([w["kind"] for w in data.json()["widgets"]],
                         ["success_rate", "host_outcomes"])

        csv = self.client.get(f"/api/v1/reports/{report_id}/data/0.csv")
        self.assertEqual(csv.status_code, 200, csv.content)
        self.assertEqual(csv["Content-Type"], "text/csv")
        self.assertEqual(csv.content.decode().splitlines()[0], "day,Patch web")

        patched = self.client.patch(f"/api/v1/reports/{report_id}/",
                                    {"description": "patching, weekly"},
                                    format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(self.client.delete(f"/api/v1/reports/{report_id}/").status_code,
                         204)
        self.assertEqual(self.client.get(f"/api/v1/reports/{report_id}/data/").status_code,
                         404)

    def test_csv_cells_never_start_a_formula(self):
        widget = {"kind": "host_outcomes", "data": {"rows": [
            {"host": "=HYPERLINK(\"http://x\")", "source": "+cmd", "outcome": "-1", "at": "@now"}]}}
        lines = widgets.to_csv(widget).splitlines()
        self.assertEqual(lines[1], "'@now,\"'=HYPERLINK(\"\"http://x\"\")\",'-1,'+cmd")
        rate = {"kind": "success_rate", "data": {"days": ["2026-10-15"],
                                                 "series": [{"name": "=sum", "values": [-5]}]}}
        self.assertEqual(widgets.to_csv(rate).splitlines(), ["day,'=sum", "2026-10-15,-5"])

    def test_csv_filename_slugs_the_report_name(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], name="Q3 — Patching!",
                             widget_kinds=("runs_per_day",))
        response = self.csv(report, 0)
        self.assertEqual(response["Content-Disposition"],
                         'attachment; filename="q3-patching-runs_per_day.csv"')


class HelperTests(ReportTestBase):
    def test_percentile_is_nearest_rank(self):
        self.assertEqual(widgets._percentile([], 0.95), None)
        self.assertEqual(widgets._percentile([10, 20, 30], 0.5), 20)
        self.assertEqual(widgets._percentile([10, 20, 30], 0.95), 30)

    def test_widget_specs_drop_unknown_kinds(self):
        report = Report(name="x", widgets=[{"kind": "success_rate"},
                                           {"kind": "nope"}, {"kind": "success_rate"},
                                           "junk"])
        self.assertEqual([kind for kind, _spec in widgets._widget_specs(report.widgets)],
                         ["success_rate", "success_rate"])

    def test_host_state_comes_from_the_shared_helper(self):
        seen = []

        def spy(tasks):
            seen.append([t.state for t in tasks])
            return "ok"

        self.addsetattr(widgets, "_host_state", spy)
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7)
        self.host_run(playbook=pb, day=TODAY, host="web", steps=(("patch", "completed"),))
        widgets.compute(report, self.admin, now=NOW)
        self.assertEqual(seen, [["completed"]])


class MutationProbes(ReportTestBase):
    """Each of these pins one arm of a widget's rule, so that dropping it —
    the denominator of the rate, the `on_failure: continue` exemption, the
    `not_applicable`/`skipped` counting as ok — turns the test red."""

    def test_rate_denominator_excludes_pending(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7, widget_kinds=("success_rate",))
        self.host_run(playbook=pb, day=TODAY, host="ok", steps=(("patch", "completed"),))
        self.host_run(playbook=pb, day=TODAY, host="late", steps=(("patch", "dispatched"),))
        self.assertEqual(self.data(report, "success_rate")["series"][0]["values"][-1],
                         100.0)

    def test_not_applicable_and_skipped_count_as_ok(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("success_rate", "runs_per_day"))
        self.host_run(playbook=pb, day=TODAY, host="a",
                      steps=(("patch", "not_applicable"),))
        self.host_run(playbook=pb, day=TODAY, host="b", steps=(("patch", "skipped"),))
        data = self.data(report)
        self.assertEqual(data["widgets"][0]["data"]["series"][0]["values"][-1], 100.0)
        self.assertEqual(data["widgets"][1]["data"]["ok"][-1], 2)

    def test_expired_and_rejected_are_failures(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("success_rate", "failures_by_step"))
        self.host_run(playbook=pb, day=TODAY, host="a", steps=(("patch", "expired"),))
        self.host_run(playbook=pb, day=TODAY, host="b", steps=(("harden", "rejected"),))
        data = self.data(report)
        self.assertEqual(data["widgets"][0]["data"]["series"][0]["values"][-1], 0.0)
        self.assertEqual([r["step"] for r in data["widgets"][1]["data"]["rows"]],
                         ["harden", "patch"])

    def test_a_handled_failure_keeps_its_host_run_ok(self):
        pb = self.playbook("Patch web")
        report = self.report(sources=[pb], range_days=7,
                             widget_kinds=("success_rate", "runs_per_day"))
        self.host_run(playbook=pb, day=TODAY, host="a",
                      steps=(("rescan", "continue"), ("harden", "completed")))
        data = self.data(report)
        self.assertEqual(data["widgets"][0]["data"]["series"][0]["values"][-1], 100.0)
        self.assertEqual(data["widgets"][1]["data"]["failed"][-1], 0)
