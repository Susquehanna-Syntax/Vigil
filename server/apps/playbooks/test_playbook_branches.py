"""If/then/else between a playbook's task steps (M6 phase 08b).

The server creates every step's task up front with a guard built from the
playbook's flow, then releases the chain: a task whose guard is false is
skipped ("branch … not taken"), the first runnable one becomes pending. A
condition reads an earlier step's status (ok / failed / not_applicable /
skipped) or its task's outputs, merged.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.playbooks.community_yaml import parse, to_yaml
from apps.playbooks.flow import FlowError, build_flow
from apps.playbooks.models import Playbook, dispatch_to_host
from apps.playbooks.views import _validate_and_set_steps
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate

CHECK = ("name: Check nginx\nrisk: low\nactions:\n"
         "  - id: find\n    type: hunt_process\n    params:\n      name: nginx\n")
PATCH = ("name: Patch nginx\nrisk: low\nactions:\n"
         "  - id: p\n    type: check_service\n    params:\n      service_name: nginx\n")
INSTALL = ("name: Install nginx\nrisk: low\nactions:\n"
           "  - id: i\n    type: check_service\n    params:\n      service_name: nginx\n")
VERIFY = ("name: Verify web\nrisk: low\nactions:\n"
          "  - id: v\n    type: check_service\n    params:\n      service_name: cron\n")
RELEVANT = ("name: Only where nginx\nrisk: low\nrelevant:\n  all:\n    - hunt_process:\n"
            "        name: nginx\nactions:\n  - id: r\n    type: check_service\n"
            "    params:\n      service_name: nginx\n")
TWO_OUTPUTS = ("name: Two outputs\nrisk: low\nactions:\n"
               "  - id: first\n    type: hunt_process\n    params:\n      name: a\n"
               "  - id: second\n    type: hunt_process\n    params:\n      name: b\n")


class BranchTestBase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pb", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.host = Host.objects.create(
            hostname="web", agent_token="tok-branches", status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED)
        self.defs = {}

    def definition(self, source):
        spec = parse_and_validate(source)
        d = TaskDefinition.objects.create(owner=self.user, name=spec["name"],
                                          yaml_source=source, parsed_spec=spec)
        self.defs[spec["name"]] = d
        return d

    def playbook(self, items, name="PB"):
        pb = Playbook.objects.create(name=name, created_by=self.user)
        err = _validate_and_set_steps(pb, items)
        self.assertIsNone(err, err and err.data)
        pb.refresh_from_db()
        return pb

    def dispatch(self, pb):
        dispatch_to_host(self.host, playbooks=[pb])
        return TaskRun.objects.filter(playbook=pb).latest("created_at")

    def by_step(self, run):
        return {t.step_ref: t for t in run.tasks.all()}

    def report(self, task, state="completed", results=None):
        """Post a result for *task*, which the chain must have released."""
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.PENDING, f"{task.step_ref} was not released")
        task.state = Task.State.DISPATCHED
        task.save(update_fields=["state"])
        steps = [{"id": sid, "status": "ok", "result": res} for sid, res in (results or [])]
        resp = self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps({"task_id": str(task.id), "state": state, "output": "done",
                             "steps": steps}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}")
        self.assertEqual(resp.status_code, 200, resp.content)

    def states(self, run):
        return {t.step_ref: t.state for t in run.tasks.all()}


class BuildFlowTests(BranchTestBase):
    def test_validate_flow_rules(self):
        check, patch = self.definition(CHECK), self.definition(PATCH)

        def refused(items, fragment):
            with self.assertRaises(FlowError) as ctx:
                build_flow(items)
            self.assertIn(fragment, str(ctx.exception))

        step = lambda d, i=None: {"definition": d, "id": i}  # noqa: E731
        # A condition may only name an earlier step.
        refused([{"if": "steps.later.status == 'ok'", "then": [step(patch)]},
                 step(check, "later")], "not an earlier playbook step")
        # …and only an output its task produces.
        refused([step(check, "c"), {"if": "steps.c.result.nope > 0", "then": [step(patch)]}],
                "has no output 'nope'")
        # Ordering needs a number: check_service's `state` is text.
        refused([step(patch, "p"), {"if": "steps.p.result.state > 0", "then": [step(check)]}],
                "compare numbers")
        # Four levels deep.
        deep = [step(patch, "x")]
        for _ in range(4):
            deep = [{"if": "steps.c.status == 'ok'", "then": deep}]
        refused([step(check, "c"), *deep], "deeper than 3")
        refused([step(check, "c"), step(patch, "c")], "duplicate step id")
        refused([step(check, "yes please")], "must start with a letter")

    def test_linear_playbooks_unchanged(self):
        pb = self.playbook([str(self.definition(CHECK).id), str(self.definition(VERIFY).id)])
        self.assertIsNone(pb.flow)
        self.assertEqual([s.step_id for s in pb.steps.order_by("order")], ["s1", "s2"])
        run = self.dispatch(pb)
        chain = list(run.tasks.order_by("step_order"))
        self.assertEqual([t.state for t in chain], [Task.State.PENDING, Task.State.BLOCKED])
        self.assertEqual([t.guard for t in chain], ["", ""])


class BranchRunTests(BranchTestBase):
    def branching(self):
        check, patch, install, verify = (self.definition(s) for s in (CHECK, PATCH, INSTALL, VERIFY))
        return self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.result.count > 0",
             "then": [{"definition_id": str(patch.id), "id": "patch"}],
             "else": [{"definition_id": str(install.id), "id": "install"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ])

    def test_then_taken(self):
        run = self.dispatch(self.branching())
        tasks = self.by_step(run)
        self.report(tasks["check"], results=[("find", {"matched": True, "count": 2, "truncated": False})])
        self.assertEqual(self.states(run)["install"], Task.State.BLOCKED)
        self.report(tasks["patch"])
        states = self.states(run)
        self.assertEqual(states["install"], Task.State.SKIPPED)
        tasks["install"].refresh_from_db()
        self.assertEqual(tasks["install"].result_output, "[SKIPPED] branch b1.else not taken")
        self.report(tasks["verify"])
        run.refresh_from_db()
        self.assertEqual(run.state, TaskRun.State.COMPLETED)

    def test_else_taken(self):
        run = self.dispatch(self.branching())
        tasks = self.by_step(run)
        self.report(tasks["check"], results=[("find", {"matched": False, "count": 0, "truncated": False})])
        states = self.states(run)
        self.assertEqual(states["patch"], Task.State.SKIPPED)
        self.assertEqual(states["install"], Task.State.PENDING)
        self.report(tasks["install"])
        self.report(tasks["verify"])

    def test_status_condition(self):
        rel, patch, verify = self.definition(RELEVANT), self.definition(PATCH), self.definition(VERIFY)
        pb = self.playbook([
            {"definition_id": str(rel.id), "id": "check", "on_not_applicable": "skip"},
            {"if": 'steps.check.status == "not_applicable"',
             "then": [{"definition_id": str(verify.id), "id": "fallback"}],
             "else": [{"definition_id": str(patch.id), "id": "fix"}]},
        ])
        run = self.dispatch(pb)
        tasks = self.by_step(run)
        self.report(tasks["check"], state="not_applicable")
        self.report(tasks["fallback"])
        # The walk reaches the else-step only after the then-step finishes.
        self.assertEqual(self.states(run)["fix"], Task.State.SKIPPED)

    def test_merged_outputs(self):
        two, patch, install = self.definition(TWO_OUTPUTS), self.definition(PATCH), self.definition(INSTALL)
        pb = self.playbook([
            {"definition_id": str(two.id), "id": "look"},
            # `count` comes from both task steps; the later one (3) must win.
            {"if": "steps.look.result.count == 3",
             "then": [{"definition_id": str(patch.id), "id": "later"}],
             "else": [{"definition_id": str(install.id), "id": "earlier"}]},
        ])
        run = self.dispatch(pb)
        self.report(self.by_step(run)["look"], results=[
            ("first", {"matched": True, "count": 1, "truncated": False}),
            ("second", {"matched": True, "count": 3, "truncated": False}),
        ])
        tasks = self.by_step(run)
        self.report(tasks["later"])
        self.assertEqual(self.states(run)["earlier"], Task.State.SKIPPED)


class BranchYamlApiTests(BranchTestBase):
    YAML = (
        "name: Web fix\nsteps:\n"
        "  - task: check-nginx\n    id: check\n"
        "  - if: steps.check.result.count > 0\n    then:\n"
        "      - task: patch-nginx\n        id: patch\n    else:\n"
        "      - task: install-nginx\n        id: install\n"
        "  - task: verify-web\n    id: verify\n")

    def test_yaml_tree_round_trips(self):
        for source in (CHECK, PATCH, INSTALL, VERIFY):
            self.definition(source)
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/playbooks/yaml/", {"yaml": self.YAML}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        pb = Playbook.objects.get(name="Web fix")
        self.assertEqual(pb.flow[1]["if"], "steps.check.result.count > 0")
        self.assertEqual([s.step_id for s in pb.steps.order_by("order")],
                         ["check", "patch", "install", "verify"])
        again = parse(to_yaml(pb))
        self.assertEqual([s["id"] for s in again["steps"]], ["check", "patch", "install", "verify"])
        self.assertEqual(again["tree"][1]["then"], [{"step": 1}])
        self.assertEqual(again["tree"][1]["else"], [{"step": 2}])

    def test_yaml_quoting_hint_for_boolean_ids(self):
        from vigil.contentyaml import ContentYamlError
        with self.assertRaisesMessage(ContentYamlError, "quote it"):
            parse("name: X\nsteps:\n  - task: a\n    id: yes\n")

    def test_api_accepts_flow_steps(self):
        check, patch = self.definition(CHECK), self.definition(PATCH)
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/playbooks/", {"name": "API PB", "flow_steps": [
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.result.count > 0",
             "then": [{"definition_id": str(patch.id), "id": "patch"}]},
        ]}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["flow"][1]["then"], [{"step": "patch"}])
        self.assertEqual([s["branch"] for s in body["steps"]], ["", "b1.then"])
        bad = client.post("/api/v1/playbooks/", {"name": "Bad", "flow_steps": [
            {"if": "steps.nowhere.status == 'ok'",
             "then": [{"definition_id": str(patch.id)}]},
        ]}, format="json")
        self.assertEqual(bad.status_code, 400)


class FailureBranchTests(BranchTestBase):
    """on_failure: continue makes a failed task a branch option (08b2)."""

    def recovering(self, on_failure="continue"):
        check, patch, install, verify = (self.definition(s) for s in (CHECK, PATCH, INSTALL, VERIFY))
        pb = self.playbook([
            {"definition_id": str(patch.id), "id": "upgrade", "on_failure": on_failure},
            {"if": 'steps.upgrade.status == "failed"',
             "then": [{"definition_id": str(install.id), "id": "reinstall"}],
             "else": [{"definition_id": str(check.id), "id": "confirm"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ])
        pb.completion_tag = "web-ok"
        pb.save()
        return pb

    def test_failure_routes_to_recovery(self):
        from apps.playbooks.models import last_outcomes

        pb = self.recovering()
        run = self.dispatch(pb)
        tasks = self.by_step(run)
        self.report(tasks["upgrade"], state="failed")
        self.assertEqual(self.states(run)["reinstall"], Task.State.PENDING)
        self.report(tasks["reinstall"])
        self.assertEqual(self.states(run)["confirm"], Task.State.SKIPPED)
        self.report(tasks["verify"])
        run.refresh_from_db()
        # The failure was handled: the run completed, the host is not held
        # back, and it earned its completion tag.
        self.assertEqual(run.state, TaskRun.State.COMPLETED)
        self.assertEqual(last_outcomes(pb)[self.host.id], "ok")
        self.host.refresh_from_db()
        self.assertIn("web-ok", self.host.tags)

    def test_default_stop_still_ends_the_chain(self):
        from apps.playbooks.models import last_outcomes

        pb = self.recovering(on_failure="stop")
        run = self.dispatch(pb)
        self.report(self.by_step(run)["upgrade"], state="failed")
        states = self.states(run)
        self.assertEqual({states["reinstall"], states["confirm"], states["verify"]},
                         {Task.State.REJECTED})
        self.assertEqual(last_outcomes(pb)[self.host.id], "failed")

    def test_on_failure_validated_and_round_trips(self):
        patch = self.definition(PATCH)
        pb = Playbook.objects.create(name="Bad", created_by=self.user)
        err = _validate_and_set_steps(pb, [{"definition_id": str(patch.id), "on_failure": "retry"}])
        self.assertEqual(err.status_code, 400)
        pb = self.recovering()
        again = parse(to_yaml(pb))
        self.assertEqual(again["steps"][0]["on_failure"], "continue")
        self.assertEqual(again["steps"][1]["on_failure"], "stop")
