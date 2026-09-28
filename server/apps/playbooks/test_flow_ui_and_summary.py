"""Run summaries, named outcomes, step colours and the flow UI (M6 08c).

Each host of a run ends in one state and — when the task or playbook names
outcomes on its steps — in the outcome of the last labelled step that
succeeded there. Run detail returns the summary; the task library shows each
task's latest one. The UI pieces are source-scanned for escaping.
"""
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.playbooks.community_yaml import parse, to_yaml
from apps.playbooks.models import Playbook, dispatch_to_host
from apps.playbooks.views import _validate_and_set_steps
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate
from apps.tasks.summary import run_summary

JS = Path(__file__).resolve().parents[2] / "static" / "js"

STEP = ("name: {name}\nrisk: low\nactions:\n"
        "  - id: {sid}\n    type: check_service\n    params:\n      service_name: cron\n")


class SummaryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sum", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.hosts = [Host.objects.create(hostname=f"h{i}", agent_token=f"tok-sum-{i}",
                                          status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
                      for i in range(3)]

    def definition(self, name, sid="s", extra=""):
        src = STEP.format(name=name, sid=sid) + extra
        return TaskDefinition.objects.create(owner=self.user, name=name, yaml_source=src,
                                             parsed_spec=parse_and_validate(src))

    def test_playbook_outcomes_and_handled_failure(self):
        a, b = self.definition("A", "a"), self.definition("B", "b")
        pb = Playbook.objects.create(name="PB", created_by=self.user)
        self.assertIsNone(_validate_and_set_steps(pb, [
            {"definition_id": str(a.id), "id": "first", "on_failure": "continue", "outcome": "Tried"},
            {"definition_id": str(b.id), "id": "second", "outcome": "Done", "color": "mint"},
        ]))
        pb.refresh_from_db()
        for host in self.hosts:
            dispatch_to_host(host, playbooks=[pb])
        runs = list(TaskRun.objects.filter(playbook=pb))
        # Every run keeps the flow it was dispatched with, colours and outcomes included.
        snap = runs[0].flow_snapshot
        self.assertEqual([(s["step_id"], s["outcome"], s["color"]) for s in snap["steps"]],
                         [("first", "Tried", ""), ("second", "Done", "mint")])
        # host 0: both ok → Done; host 1: first failed (handled) → second ok → Done;
        # host 2: first ok, second still waiting → Tried.
        for i, (first, second) in enumerate(((Task.State.COMPLETED, Task.State.COMPLETED),
                                             (Task.State.FAILED, Task.State.COMPLETED),
                                             (Task.State.COMPLETED, Task.State.PENDING))):
            chain = list(Task.objects.filter(host=self.hosts[i]).order_by("step_order"))
            chain[0].state, chain[1].state = first, second
            chain[0].save(); chain[1].save()
        summaries = [run_summary(r) for r in runs]
        states = {s["per_host"][str(h.id)]["state"] for s in summaries for h in self.hosts
                  if str(h.id) in s["per_host"]}
        outcomes = sorted(o for s in summaries for o in (s["outcomes"] or {}))
        self.assertEqual(states, {"ok", "pending"})
        self.assertEqual(outcomes, ["Done", "Done", "Tried"])

    def test_task_outcomes_and_library_last_run(self):
        d = self.definition("Patch", "patch", extra="    outcome: Patched\n")
        self.assertEqual(d.parsed_spec["actions"][0]["outcome"], "Patched")
        run = TaskRun.objects.create(definition=d, name_snapshot="Patch", requested_by=self.user,
                                     host_count=2, step_count=1)
        for host, state, status in ((self.hosts[0], "completed", "ok"), (self.hosts[1], "failed", "error")):
            Task.objects.create(host=host, run=run, action="_script", params={}, state=state,
                                nonce=f"n-{host.id}", result_data={"steps": [
                                    {"id": "patch", "status": status, "result": {}}]})
        summary = run_summary(run)
        self.assertEqual(summary["states"], {"ok": 1, "failed": 1})
        self.assertEqual(summary["outcomes"], {"Patched": 1, "No outcome yet": 1})
        client = APIClient()
        client.force_authenticate(self.user)
        detail = client.get(f"/api/v1/tasks/runs/{run.id}/").json()
        self.assertEqual(detail["summary"]["states"], {"ok": 1, "failed": 1})
        row = next(r for r in client.get("/api/v1/tasks/definitions/").json() if r["id"] == str(d.id))
        self.assertEqual(row["last_run"]["states"], {"ok": 1, "failed": 1})
        self.assertNotIn("per_host", row["last_run"])

    def test_color_and_outcome_validated_and_round_trip(self):
        a = self.definition("A", "a")
        pb = Playbook.objects.create(name="PB2", created_by=self.user)
        self.assertEqual(_validate_and_set_steps(
            pb, [{"definition_id": str(a.id), "color": "chartreuse"}]).status_code, 400)
        self.assertEqual(_validate_and_set_steps(
            pb, [{"definition_id": str(a.id), "outcome": "x" * 41}]).status_code, 400)
        self.assertIsNone(_validate_and_set_steps(
            pb, [{"definition_id": str(a.id), "color": "peach", "outcome": "Checked"}]))
        again = parse(to_yaml(pb))
        self.assertEqual((again["steps"][0]["color"], again["steps"][0]["outcome"]),
                         ("peach", "Checked"))


class FlowUiSourceTests(SimpleTestCase):
    def src(self, name):
        return (JS / name).read_text()

    def test_flow_ui_escapes_what_it_draws(self):
        flow = self.src("vigil-playbook-flow.js")
        # Step names, ids, conditions and outcomes are author- or agent-supplied.
        for needle in ("escHtml(s.name || s.def)", "escHtml(s.sid)", "escHtml(node.cond)",
                       "escHtml(s.outcome)", "escAttr(b.cond || '')"):
            self.assertIn(needle, flow)
        charts = self.src("vigil-charts.js")
        self.assertIn("escHtml(s.label)", charts)

    def test_editor_is_wired(self):
        playbooks = self.src("vigil-playbooks.js")
        self.assertIn("flow_steps: flowSerialize(_editingTree)", playbooks)
        self.assertIn("flowAnalysisHtml(", playbooks)
        self.assertIn("[data-flow-outcome]", playbooks)
        base = (JS.parents[1] / "templates" / "base.html").read_text()
        self.assertLess(base.index("vigil-playbook-flow.js"), base.index("vigil-playbooks.js"))
        self.assertIn("vigil-charts.js", base)

    def test_findings_catch_an_unreachable_failure_branch(self):
        flow = self.src("vigil-playbook-flow.js")
        self.assertIn("can never be true: when ${ref.sid} fails the playbook stops there", flow)
