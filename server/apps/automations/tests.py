import json
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
            action_kind="baseline", baseline=b, target="event_host",
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
        # The baseline reference points nowhere (deleted) → handle_event must not raise.
        Automation.objects.create(
            name="broken", trigger="event", event="host_approved",
            action_kind="baseline", baseline=None, target="event_host",
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

    def test_params_override_round_trips_and_applies(self):
        d = make_def(actions=[{"type": "restart_service",
                               "params": {"service_name": "nginx"}}])
        a = Automation.objects.create(name="svc", trigger="event", event="alert_fired",
                                      action_kind="task", task_definition=d,
                                      target="all", created_by=self.admin)
        resp = self.client.patch(f"/api/v1/automations/{a.id}/", {
            "params_override": {"0": {"service_name": "postgres"}}},
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["params_override"],
                         {"0": {"service_name": "postgres"}})
        host = make_host()
        a.refresh_from_db()
        self.assertEqual(run_automation(a), 1)
        task = Task.objects.get(host=host)
        self.assertEqual(task.params["steps"][0]["params"],
                         {"service_name": "postgres"})

    def test_unknown_override_param_rejected(self):
        d = make_def(actions=[{"type": "restart_service",
                               "params": {"service_name": "nginx"}}])
        a = Automation.objects.create(name="bad-ov", trigger="event", event="alert_fired",
                                      action_kind="task", task_definition=d,
                                      target="event_host", created_by=self.admin)
        resp = self.client.patch(f"/api/v1/automations/{a.id}/", {
            "params_override": {"0": {"bogus": "x"}}},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("bogus", resp.json()["detail"])

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


class AutomationBaselineReferenceTests(TestCase):
    """The automation must point at ONE specific baseline, not a name that
    could match several once baselines become site-scoped."""

    def setUp(self):
        self.defn = make_def("echo")

    def test_automation_resolves_its_own_baseline_not_a_namesake(self):
        first = Baseline.objects.create(name="Nightly patch scan")
        BaselineStep.objects.create(baseline=first, definition=self.defn, order=0)

        # A second baseline that a name lookup could ambiguously match.
        second = Baseline.objects.create(name="nightly patch scan (west)")
        BaselineStep.objects.create(baseline=second, definition=self.defn, order=0)

        auto = Automation.objects.create(
            name="patch", trigger=Automation.Trigger.SCHEDULE,
            action_kind=Automation.ActionKind.BASELINE, baseline=second,
        )
        auto.refresh_from_db()
        self.assertEqual(auto.baseline_id, second.id)
        self.assertNotEqual(auto.baseline_id, first.id)

    def test_deleting_the_baseline_nulls_the_reference(self):
        b = Baseline.objects.create(name="temp")
        auto = Automation.objects.create(
            name="a", trigger=Automation.Trigger.SCHEDULE,
            action_kind=Automation.ActionKind.BASELINE, baseline=b,
        )
        b.delete()
        auto.refresh_from_db()
        self.assertIsNone(auto.baseline_id)


class RunHistoryTests(TestCase):
    """Automation and baseline dispatches must be visible as runs, with a
    resolved outcome — not a scatter of orphan tasks."""

    def setUp(self):
        self.admin = get_user_model().objects.create_user("hist", password="x", is_staff=True)
        self.client.force_login(self.admin)
        wire()

    def _fire(self):
        d = make_def()
        Automation.objects.create(
            name="nightly", trigger="event", event="host_approved",
            action_kind="task", task_definition=d, target="event_host",
            created_by=self.admin)
        host = make_host("run-01")
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        return host

    def test_an_automation_dispatch_creates_one_run(self):
        from apps.tasks.models import TaskRun
        self._fire()
        run = TaskRun.objects.get()
        self.assertEqual(run.source, TaskRun.Source.AUTOMATION)
        self.assertEqual(run.automation.name, "nightly")
        self.assertEqual(run.host_count, 1)
        self.assertEqual(run.state, TaskRun.State.RUNNING)

    def test_its_tasks_belong_to_that_run(self):
        from apps.tasks.models import Task, TaskRun
        self._fire()
        run = TaskRun.objects.get()
        self.assertEqual(Task.objects.filter(run=run).count(), 1)

    def test_the_run_resolves_when_the_task_reports_back(self):
        from apps.tasks.models import Task, TaskRun
        host = self._fire()
        task = Task.objects.get(run__isnull=False)
        task.state = Task.State.DISPATCHED
        task.save(update_fields=["state"])
        resp = self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps({"task_id": str(task.id),
                             "state": "completed", "output": "done"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {host.agent_token}")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        run = TaskRun.objects.get()
        self.assertEqual(run.state, TaskRun.State.COMPLETED)
        self.assertIsNotNone(run.finished_at)

    def test_history_endpoint_filters_by_source(self):
        self._fire()
        rows = self.client.get("/api/v1/tasks/runs/?source=automation").json()
        self.assertEqual(rows["count"], 1)
        self.assertEqual(rows["results"][0]["automation_name"], "nightly")
        self.assertEqual(self.client.get(
            "/api/v1/tasks/runs/?source=baseline").json()["count"], 0)

    def test_history_endpoint_rejects_an_unknown_source(self):
        self.assertEqual(self.client.get(
            "/api/v1/tasks/runs/?source=nonsense").status_code, 400)

    def test_deleting_the_automation_keeps_the_history_row(self):
        from apps.tasks.models import TaskRun
        self._fire()
        Automation.objects.get(name="nightly").delete()
        run = TaskRun.objects.get()
        self.assertIsNone(run.automation_id)
        self.assertEqual(run.source, TaskRun.Source.AUTOMATION)
        self.assertIn("nightly", run.name_snapshot)


class EventTextAndHostFilterTests(TestCase):
    """Narrow which alerts fire an automation: by text, and by host.

    Both scope the TRIGGER, not the target. An automation can watch one host
    and act on another, so `event_host` is deliberately separate from
    `target_host`.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        wire()
        self.definition = make_def()

    def _automation(self, **kw):
        kw.setdefault("target", "event_host")
        return Automation.objects.create(
            name="a", trigger="event", event="alert_fired",
            action_kind="task", task_definition=self.definition,
            created_by=self.admin, **kw)

    def _fire(self, host, *, rule_name="disk full", message="/var is at 95%"):
        rule = AlertRule.objects.create(
            name=rule_name, category="disk", metric="d", operator="gt",
            threshold=90, severity="critical")
        alert = Alert.objects.create(host=host, rule=rule,
                                     severity="critical", message=message)
        hooks.emit("alert_fired", alert=alert)
        return alert

    def _ran(self, host):
        return Task.objects.filter(host=host).exists()

    # ── contains ───────────────────────────────────────────────────────
    def test_contains_matches_the_description(self):
        self._automation(match_text="/var", match_mode="contains")
        host = make_host("h1")
        self._fire(host, message="/var is at 95%")
        self.assertTrue(self._ran(host))

    def test_contains_matches_the_name(self):
        self._automation(match_text="disk", match_mode="contains")
        host = make_host("h2")
        self._fire(host, rule_name="disk full", message="nothing relevant")
        self.assertTrue(self._ran(host))

    def test_contains_does_not_fire_when_absent(self):
        self._automation(match_text="postgres", match_mode="contains")
        host = make_host("h3")
        self._fire(host)
        self.assertFalse(self._ran(host))

    def test_matching_is_case_insensitive(self):
        self._automation(match_text="DISK", match_mode="contains")
        host = make_host("h4")
        self._fire(host, rule_name="disk full")
        self.assertTrue(self._ran(host))

    # ── does not contain ───────────────────────────────────────────────
    def test_not_contains_fires_when_absent(self):
        self._automation(match_text="backup", match_mode="not_contains")
        host = make_host("h5")
        self._fire(host)
        self.assertTrue(self._ran(host))

    def test_not_contains_suppresses_when_present(self):
        self._automation(match_text="backup", match_mode="not_contains")
        host = make_host("h6")
        self._fire(host, message="backup volume is at 95%")
        self.assertFalse(self._ran(host))

    def test_not_contains_over_both_fields_means_neither(self):
        """A filter meant to exclude something must not let it through
        because it matched the field the operator wasn't thinking about."""
        self._automation(match_text="backup", match_mode="not_contains",
                         match_field="any")
        host = make_host("h7")
        self._fire(host, rule_name="backup disk", message="/var is at 95%")
        self.assertFalse(self._ran(host))

    # ── field scoping ──────────────────────────────────────────────────
    def test_field_message_ignores_the_name(self):
        self._automation(match_text="disk", match_mode="contains",
                         match_field="message")
        host = make_host("h8")
        self._fire(host, rule_name="disk full", message="/var is at 95%")
        self.assertFalse(self._ran(host))

    def test_field_rule_ignores_the_description(self):
        self._automation(match_text="/var", match_mode="contains",
                         match_field="rule")
        host = make_host("h9")
        self._fire(host, rule_name="disk full", message="/var is at 95%")
        self.assertFalse(self._ran(host))

    def test_an_alert_with_no_rule_has_an_empty_name(self):
        """Vigil raises some alerts directly, with rule=None."""
        self._automation(match_text="disk", match_mode="contains",
                         match_field="rule")
        host = make_host("h10")
        alert = Alert.objects.create(host=host, rule=None, severity="warning",
                                     message="Agent outdated: disk")
        hooks.emit("alert_fired", alert=alert)
        self.assertFalse(self._ran(host))

    def test_blank_text_means_no_filter(self):
        self._automation(match_text="", match_mode="contains")
        host = make_host("h11")
        self._fire(host)
        self.assertTrue(self._ran(host))

    # ── host scope ─────────────────────────────────────────────────────
    def test_event_host_scope_fires_for_that_host(self):
        watched = make_host("watched")
        self._automation(event_host=watched)
        self._fire(watched)
        self.assertTrue(self._ran(watched))

    def test_event_host_scope_ignores_other_hosts(self):
        watched = make_host("watched2")
        other = make_host("other2")
        self._automation(event_host=watched)
        self._fire(other)
        self.assertFalse(self._ran(other))

    def test_no_event_host_means_any_host(self):
        self._automation()
        host = make_host("anyhost")
        self._fire(host)
        self.assertTrue(self._ran(host))

    def test_host_scope_and_text_filter_both_apply(self):
        watched = make_host("both")
        self._automation(event_host=watched, match_text="postgres",
                         match_mode="contains")
        self._fire(watched, message="/var is at 95%")
        self.assertFalse(self._ran(watched), "text filter should have blocked")

    def test_watching_one_host_can_act_on_another(self):
        """event_host scopes the trigger; target decides where it runs."""
        watched = make_host("sensor")
        actor = make_host("actor")
        self._automation(event_host=watched, target="host", target_host=actor)
        self._fire(watched)
        self.assertTrue(self._ran(actor))
        self.assertFalse(self._ran(watched))


class EventFilterApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.definition = make_def()

    def _create(self, **extra):
        body = {"name": "a", "trigger": "event", "event": "alert_fired",
                "action_kind": "task",
                "task_definition": str(self.definition.id),
                "target": "event_host", **extra}
        return self.client.post("/api/v1/automations/", json.dumps(body),
                                content_type="application/json")

    def test_the_filters_round_trip(self):
        host = make_host("api-host")
        resp = self._create(match_text="/var", match_field="message",
                            match_mode="not_contains",
                            event_host=str(host.id))
        self.assertEqual(resp.status_code, 201, resp.content)
        row = self.client.get("/api/v1/automations/").json()["automations"][0]
        self.assertEqual(row["match_text"], "/var")
        self.assertEqual(row["match_field"], "message")
        self.assertEqual(row["match_mode"], "not_contains")
        self.assertEqual(row["event_host"], str(host.id))
        self.assertEqual(row["event_host_name"], "api-host")

    def test_an_invalid_match_mode_is_refused(self):
        resp = self._create(match_mode="regex")
        self.assertEqual(resp.status_code, 400)

    def test_an_invalid_match_field_is_refused(self):
        resp = self._create(match_field="hostname")
        self.assertEqual(resp.status_code, 400)
