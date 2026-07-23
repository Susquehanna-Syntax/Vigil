import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from vigil import hooks

from .apps import wire
from .expansion import BaselineExpandError, expand_actions
from .models import Baseline, BaselineStep, dispatch_to_host, eligible


def make_definition(*, risk="standard", actions=None, name=None):
    return TaskDefinition.objects.create(
        name=name or f"def-{uuid.uuid4().hex[:6]}",
        risk_level=risk,
        yaml_source="",
        parsed_spec={
            "risk": risk,
            "actions": actions if actions is not None else [
                {"type": "pkg_update", "params": {}},
            ],
        },
    )


def make_baseline(admin, *, name=None, definitions=None, tags=None, enabled=True):
    b = Baseline.objects.create(
        name=name or f"bl-{uuid.uuid4().hex[:6]}",
        created_by=admin, target_tags=tags or [], enabled=enabled)
    for i, d in enumerate(definitions or [make_definition()]):
        BaselineStep.objects.create(baseline=b, definition=d, order=i)
    return b


def make_host(mode=Host.Mode.MANAGED, tags=None):
    return Host.objects.create(
        hostname=f"h-{uuid.uuid4().hex[:6]}", mode=mode,
        status=Host.Status.ONLINE, tags=tags or [],
        agent_token=uuid.uuid4().hex,
    )


class BaselineDispatchTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)

    def test_approved_host_gets_full_sequence_in_order(self):
        d1 = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        d2 = make_definition(actions=[{"type": "restart_service",
                                       "params": {"service_name": "nginx"}}])
        make_baseline(self.admin, name="Linux bootstrap", definitions=[d1, d2])
        host = make_host()
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        task = Task.objects.get(host=host)
        self.assertEqual(task.step_label, "baseline: Linux bootstrap")
        self.assertEqual([s["action"] for s in task.params["steps"]],
                         ["pkg_update", "restart_service"])
        self.assertEqual(task.params["steps"][0]["id"], "step1")
        self.assertEqual(task.params["steps"][1]["id"], "step2")

    def test_monitor_mode_hosts_are_skipped(self):
        make_baseline(self.admin)
        host = make_host(mode=Host.Mode.MONITOR)
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        self.assertFalse(Task.objects.filter(host=host).exists())

    def test_tag_filter(self):
        b = make_baseline(self.admin, tags=["os:linux"])
        linux = make_host(tags=["os:linux"])
        windows = make_host(tags=["os:windows"])
        self.assertEqual(dispatch_to_host(linux, baselines=[b]), 1)
        self.assertEqual(dispatch_to_host(windows, baselines=[b]), 0)

    def test_high_risk_and_update_agent_are_ineligible(self):
        self.assertFalse(eligible(make_definition(risk="high"))[0])
        self.assertFalse(eligible(make_definition(
            actions=[{"type": "update_agent", "params": {}}]))[0])


class BaselineAsFunctionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)

    def test_baseline_ref_expands_inline(self):
        inner = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        make_baseline(self.admin, name="Common prep", definitions=[inner])
        actions, risk = expand_actions([
            {"type": "baseline", "params": {"name": "common PREP"}},  # case-insensitive
            {"type": "restart_service", "params": {"service_name": "app"}},
        ])
        self.assertEqual([a["type"] for a in actions],
                         ["pkg_update", "restart_service"])
        self.assertEqual(risk, "standard")

    def test_nested_baselines_expand(self):
        leaf = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        make_baseline(self.admin, name="Leaf", definitions=[leaf])
        mid = make_definition(actions=[{"type": "baseline", "params": {"name": "Leaf"}}])
        make_baseline(self.admin, name="Mid", definitions=[mid])
        actions, _ = expand_actions([{"type": "baseline", "params": {"name": "Mid"}}])
        self.assertEqual([a["type"] for a in actions], ["pkg_update"])

    def test_cycles_are_refused(self):
        d = make_definition(actions=[{"type": "baseline", "params": {"name": "Ouro"}}])
        make_baseline(self.admin, name="Ouro", definitions=[d])
        with self.assertRaises(BaselineExpandError):
            expand_actions([{"type": "baseline", "params": {"name": "Ouro"}}])

    def test_unknown_baseline_is_an_error(self):
        with self.assertRaises(BaselineExpandError):
            expand_actions([{"type": "baseline", "params": {"name": "ghost"}}])

    def test_disabled_baseline_is_still_callable(self):
        inner = make_definition()
        make_baseline(self.admin, name="Retired", definitions=[inner], enabled=False)
        actions, _ = expand_actions([{"type": "baseline", "params": {"name": "Retired"}}])
        self.assertEqual(len(actions), 1)

    def test_risk_escalates_to_max_of_expansion(self):
        risky = make_definition(risk="standard")
        make_baseline(self.admin, name="Std", definitions=[risky])
        _, risk = expand_actions([{"type": "baseline", "params": {"name": "Std"}}])
        self.assertEqual(risk, "standard")


class BaselineApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)

    def test_create_sequence_and_reorder(self):
        d1, d2 = make_definition(name="A"), make_definition(name="B")
        resp = self.client.post("/api/v1/baselines/", {
            "name": "Bootstrap", "definition_ids": [str(d1.id), str(d2.id)],
            "target_tags": ["os:linux"]}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        bid = resp.json()["id"]
        self.assertEqual([s["definition_name"] for s in resp.json()["steps"]],
                         ["A", "B"])
        resp = self.client.patch(f"/api/v1/baselines/{bid}/", {
            "definition_ids": [str(d2.id), str(d1.id)]},
            content_type="application/json")
        self.assertEqual([s["definition_name"] for s in resp.json()["steps"]],
                         ["B", "A"])

    def test_create_refuses_ineligible_definition(self):
        bad = make_definition(risk="high")
        resp = self.client.post("/api/v1/baselines/", {
            "name": "Nope", "definition_ids": [str(bad.id)]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Baseline.objects.count(), 0)

    def test_duplicate_name_refused(self):
        make_baseline(self.admin, name="Taken")
        d = make_definition()
        resp = self.client.post("/api/v1/baselines/", {
            "name": "taken", "definition_ids": [str(d.id)]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_step_params_override_round_trips(self):
        d = make_definition(actions=[{"type": "restart_service",
                                      "params": {"service_name": "nginx"}}])
        resp = self.client.post("/api/v1/baselines/", {
            "name": "Overridden",
            "definition_ids": [{"definition_id": str(d.id),
                                "params_override": {"0": {"service_name": "postgres"}}}]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["steps"][0]["params_override"],
                         {"0": {"service_name": "postgres"}})
        got = self.client.get(f"/api/v1/baselines/{resp.json()['id']}/").json()
        self.assertEqual(got["steps"][0]["params_override"],
                         {"0": {"service_name": "postgres"}})

    def test_step_params_override_applies_at_dispatch(self):
        from .models import build_agent_steps
        d = make_definition(actions=[
            {"type": "restart_service", "params": {"service_name": "nginx"}},
            {"type": "pkg_update", "params": {}},
        ])
        b = make_baseline(self.admin, definitions=[d])
        step = b.steps.get()
        step.params_override = {"0": {"service_name": "postgres"}}
        step.save()
        steps, _ = build_agent_steps(b)
        self.assertEqual(steps[0]["params"], {"service_name": "postgres"})
        self.assertEqual(steps[1]["params"], {})

    def test_unknown_override_param_is_refused(self):
        d = make_definition(actions=[{"type": "restart_service",
                                      "params": {"service_name": "nginx"}}])
        resp = self.client.post("/api/v1/baselines/", {
            "name": "Bad override",
            "definition_ids": [{"definition_id": str(d.id),
                                "params_override": {"0": {"bogus": "x"}}}]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("bogus", resp.json()["detail"])
        self.assertEqual(Baseline.objects.count(), 0)

    def test_toggle_and_delete(self):
        b = make_baseline(self.admin)
        resp = self.client.patch(f"/api/v1/baselines/{b.id}/", {"enabled": False},
                                 content_type="application/json")
        self.assertFalse(resp.json()["enabled"])
        self.assertEqual(self.client.delete(f"/api/v1/baselines/{b.id}/").status_code, 204)
