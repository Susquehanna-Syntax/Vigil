import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.alerts.models import Alert, AlertRule
from apps.baselines.models import Baseline, BaselineStep
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from vigil import hooks

from .apps import wire
from .engine import handle_event, run_automation
from .models import Automation


def make_def(name="pkg", risk="standard", actions=None):
    return TaskDefinition.objects.create(
        name=name, risk_level=risk, yaml_source="",
        parsed_spec={"risk": risk, "actions": actions or [{"type": "pkg_update", "params": {}}]})


def make_host(name=None, mode=Host.Mode.MANAGED, tags=None, status=Host.Status.ONLINE):
    return Host.objects.create(hostname=name or f"h-{uuid.uuid4().hex[:6]}",
                               mode=mode, status=status, tags=tags or [],
                               agent_token=uuid.uuid4().hex)


class EventAutomationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("root", password="x", is_staff=True)
        wire()

    def test_alert_fires_task_on_event_host(self):
        d = make_def()
        Automation.objects.create(
            name="disk cleanup", trigger="event", event="alert_fired",
            action_kind="task", task_definition=d, target="event_host",
            created_by=self.admin)
        host = make_host("web-01")
        rule = AlertRule.objects.create(name="disk", category="disk", metric="d",
                                        operator="gt", threshold=90, severity="critical")
        alert = Alert.objects.create(host=host, rule=rule, severity="critical", message="full")
        hooks.emit("alert_fired", alert=alert)
        task = Task.objects.get(host=host)
        self.assertIn("automation: disk cleanup", task.step_label)
        self.assertEqual(task.params["steps"][0]["action"], "pkg_update")

    def test_severity_filter(self):
        d = make_def()
        Automation.objects.create(
            name="crit only", trigger="event", event="alert_fired",
            min_severity="critical", action_kind="task", task_definition=d,
            target="event_host", created_by=self.admin)
        host = make_host()
        rule = AlertRule.objects.create(name="m", category="c", metric="m",
                                        operator="gt", threshold=1, severity="warning")
        warn = Alert.objects.create(host=host, rule=rule, severity="warning", message="w")
        hooks.emit("alert_fired", alert=warn)
        self.assertFalse(Task.objects.filter(host=host).exists())

    def test_event_tag_filter(self):
        d = make_def()
        Automation.objects.create(
            name="linux only", trigger="event", event="host_approved",
            event_tags=["os:linux"], action_kind="task", task_definition=d,
            target="event_host", created_by=self.admin)
        linux = make_host(tags=["os:linux"])
        windows = make_host(tags=["os:windows"])
        hooks.emit("host_approved", host=linux, approved_by=self.admin)
        hooks.emit("host_approved", host=windows, approved_by=self.admin)
        self.assertTrue(Task.objects.filter(host=linux).exists())
        self.assertFalse(Task.objects.filter(host=windows).exists())

    def test_baseline_action_expands(self):
        d1, d2 = make_def("a"), make_def("b", actions=[{"type": "restart_service", "params": {"service_name": "nginx"}}])
        # auto-enroll off so only the automation dispatches (baseline stays callable)
        b = Baseline.objects.create(name="Bootstrap", enabled=False, created_by=self.admin)
        BaselineStep.objects.create(baseline=b, definition=d1, order=0)
        BaselineStep.objects.create(baseline=b, definition=d2, order=1)
        Automation.objects.create(
            name="bootstrap new", trigger="event", event="host_approved",
            action_kind="baseline", baseline_name="Bootstrap", target="event_host",
            created_by=self.admin)
        host = make_host()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        task = Task.objects.get(host=host)
        self.assertIn("automation: bootstrap new", task.step_label)
        self.assertEqual([s["action"] for s in task.params["steps"]],
                         ["pkg_update", "restart_service"])

    def test_disabled_automation_does_nothing(self):
        d = make_def()
        Automation.objects.create(
            name="off", trigger="event", event="host_approved", enabled=False,
            action_kind="task", task_definition=d, target="event_host", created_by=self.admin)
        host = make_host()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        self.assertFalse(Task.objects.filter(host=host).exists())

    def test_broken_action_never_breaks_the_event(self):
        # baseline_name points nowhere → handle_event must not raise.
        Automation.objects.create(
            name="broken", trigger="event", event="host_approved",
            action_kind="baseline", baseline_name="ghost", target="event_host",
            created_by=self.admin)
        host = make_host()
        handle_event("host_approved", {"host": host})  # no exception = pass
        self.assertFalse(Task.objects.filter(host=host).exists())


class ScheduledAutomationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("root", password="x", is_staff=True)

    def test_run_dispatches_to_tag_targets(self):
        d = make_def()
        auto = Automation.objects.create(
            name="nightly", trigger="schedule", cron_hour="2",
            action_kind="task", task_definition=d, target="tags",
            target_tags=["role:backup"], created_by=self.admin)
        backup = make_host(tags=["role:backup"])
        other = make_host(tags=["role:web"])
        n = run_automation(auto)
        self.assertEqual(n, 1)
        self.assertTrue(Task.objects.filter(host=backup).exists())
        self.assertFalse(Task.objects.filter(host=other).exists())
        auto.refresh_from_db()
        self.assertEqual(auto.run_count, 1)
        self.assertIsNotNone(auto.last_run)


class AutomationApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(self.admin)

    def test_create_event_automation(self):
        d = make_def()
        resp = self.client.post("/api/v1/automations/", {
            "name": "cleanup", "trigger": "event", "event": "alert_fired",
            "action_kind": "task", "task_definition": str(d.id), "target": "event_host"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["event"], "alert_fired")

    def test_create_schedule_syncs_periodic_task(self):
        d = make_def()
        resp = self.client.post("/api/v1/automations/", {
            "name": "nightly", "trigger": "schedule",
            "cron": {"minute": "0", "hour": "2"},
            "action_kind": "task", "task_definition": str(d.id),
            "target": "all"}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        from django_celery_beat.models import PeriodicTask
        self.assertTrue(PeriodicTask.objects.filter(
            name=f"automation:{resp.json()['id']}").exists())

    def test_scheduled_event_host_target_rejected(self):
        d = make_def()
        resp = self.client.post("/api/v1/automations/", {
            "name": "bad", "trigger": "schedule", "cron": {"hour": "2"},
            "action_kind": "task", "task_definition": str(d.id), "target": "event_host"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_unknown_event_rejected(self):
        d = make_def()
        resp = self.client.post("/api/v1/automations/", {
            "name": "x", "trigger": "event", "event": "made_up",
            "action_kind": "task", "task_definition": str(d.id), "target": "event_host"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_toggle_and_delete(self):
        d = make_def()
        a = Automation.objects.create(name="a", trigger="event", event="alert_fired",
                                      action_kind="task", task_definition=d, target="event_host")
        resp = self.client.patch(f"/api/v1/automations/{a.id}/", {"enabled": False},
                                 content_type="application/json")
        self.assertFalse(resp.json()["enabled"])
        self.assertEqual(self.client.delete(f"/api/v1/automations/{a.id}/").status_code, 204)

    def test_requires_admin(self):
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.get("/api/v1/automations/").status_code, 403)


class SpecificEventTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("root2", password="x", is_staff=True)
        wire()

    def _rule(self, name, sev="critical"):
        return AlertRule.objects.create(name=name, category="disk", metric="d",
                                        operator="gt", threshold=90, severity=sev)

    def test_specific_rule_only_fires_for_that_rule(self):
        d = make_def()
        disk_rule = self._rule("Disk critical")
        mem_rule = self._rule("Memory high", "warning")
        Automation.objects.create(
            name="disk only", trigger="event", event="alert_fired",
            event_rule=disk_rule, action_kind="task", task_definition=d,
            target="event_host", created_by=self.admin)
        host = make_host()
        # a memory alert must NOT trigger it
        mem_alert = Alert.objects.create(host=host, rule=mem_rule, severity="warning", message="m")
        hooks.emit("alert_fired", alert=mem_alert)
        self.assertFalse(Task.objects.filter(host=host).exists())
        # the disk alert does
        disk_alert = Alert.objects.create(host=host, rule=disk_rule, severity="critical", message="d")
        hooks.emit("alert_fired", alert=disk_alert)
        self.assertTrue(Task.objects.filter(host=host).exists())

    def test_rule_list_endpoint(self):
        self._rule("Disk critical")
        self.client.force_login(self.admin)
        rows = self.client.get("/api/v1/alerts/rules/").json()
        self.assertTrue(any(r["name"] == "Disk critical" for r in rows))

    def test_api_sets_event_rule(self):
        d = make_def()
        rule = self._rule("CPU spike")
        self.client.force_login(self.admin)
        resp = self.client.post("/api/v1/automations/", {
            "name": "cpu", "trigger": "event", "event": "alert_fired",
            "event_rule": str(rule.id), "action_kind": "task",
            "task_definition": str(d.id), "target": "event_host"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["event_rule_name"], "CPU spike")
