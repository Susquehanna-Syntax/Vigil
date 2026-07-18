import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from vigil import hooks

from .apps import wire
from .models import Baseline, dispatch_to_host, eligible


def make_definition(*, risk="standard", actions=None, name="Update packages"):
    return TaskDefinition.objects.create(
        name=name,
        risk_level=risk,
        yaml_source="",
        parsed_spec={
            "actions": actions if actions is not None else [
                {"type": "pkg_update", "params": {}},
            ],
        },
    )


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

    def test_approved_host_gets_baseline_task(self):
        Baseline.objects.create(definition=make_definition(), created_by=self.admin)
        host = make_host()
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        task = Task.objects.get(host=host)
        self.assertEqual(task.action, "_script")
        self.assertEqual(task.state, Task.State.PENDING)
        self.assertEqual(task.params["steps"][0]["action"], "pkg_update")
        self.assertTrue(task.step_label.startswith("baseline:"))

    def test_monitor_mode_hosts_are_skipped(self):
        Baseline.objects.create(definition=make_definition(), created_by=self.admin)
        host = make_host(mode=Host.Mode.MONITOR)
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        self.assertFalse(Task.objects.filter(host=host).exists())

    def test_tag_filter(self):
        b = Baseline.objects.create(
            definition=make_definition(), created_by=self.admin,
            target_tags=["os:linux"])
        linux = make_host(tags=["os:linux"])
        windows = make_host(tags=["os:windows"])
        self.assertEqual(dispatch_to_host(linux, baselines=[b]), 1)
        self.assertEqual(dispatch_to_host(windows, baselines=[b]), 0)

    def test_high_risk_and_update_agent_are_ineligible(self):
        self.assertFalse(eligible(make_definition(risk="high"))[0])
        self.assertFalse(eligible(make_definition(
            actions=[{"type": "update_agent", "params": {}}]))[0])

    def test_api_refuses_ineligible_definition(self):
        self.client.force_login(self.admin)
        bad = make_definition(risk="high", name="danger")
        resp = self.client.post("/api/v1/baselines/",
                                {"definition_id": str(bad.id)})
        self.assertEqual(resp.status_code, 400)

    def test_api_crud(self):
        self.client.force_login(self.admin)
        d = make_definition()
        resp = self.client.post("/api/v1/baselines/", {"definition_id": str(d.id)})
        self.assertEqual(resp.status_code, 201, resp.content)
        bid = resp.json()["id"]
        resp = self.client.patch(f"/api/v1/baselines/{bid}/", {"enabled": False},
                                 content_type="application/json")
        self.assertFalse(resp.json()["enabled"])
        self.assertEqual(
            self.client.delete(f"/api/v1/baselines/{bid}/").status_code, 204)

    def test_broken_baseline_never_breaks_approval(self):
        b = Baseline.objects.create(definition=make_definition(actions=[{}]),
                                    created_by=self.admin)
        host = make_host()
        # actions entry without "type" raises KeyError inside — swallowed.
        self.assertEqual(dispatch_to_host(host, baselines=[b]), 0)
