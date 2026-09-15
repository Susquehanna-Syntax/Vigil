"""Tests for the automation condition language.

The filters an automation had before were a fixed set, all ANDed, with no way
to compare a number. What is worth proving here is the general form: that OR
works, that severity orders correctly, and — most of all — that a condition
which cannot be evaluated refuses to match rather than letting the event
through.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.automations import conditions
from apps.automations.engine import handle_event
from apps.automations.models import Automation
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition


class ConditionEvalTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="web1", agent_token="t" * 32, tags=["prod", "linux"],
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )
        self.rule = AlertRule.objects.create(
            name="Disk almost full", category="disk", metric="disk_percent",
            operator="gt", threshold=90.0, severity=AlertRule.Severity.CRITICAL,
        )
        self.alert = Alert.objects.create(
            host=self.host, rule=self.rule, severity="critical",
            message="/var is at 97%", metric_value=97.0, flap_count=3,
        )
        self.payload = {"alert": self.alert, "host": self.host,
                        "__event__": "alert_sent"}

    def check(self, field, op, value):
        return conditions.evaluate_one(
            {"field": field, "op": op, "value": value}, self.payload)

    def test_text_operators(self):
        self.assertTrue(self.check("message", "contains", "/var"))
        self.assertFalse(self.check("message", "not_contains", "/var"))
        self.assertTrue(self.check("message", "starts_with", "/var"))
        self.assertTrue(self.check("message", "ends_with", "97%"))
        self.assertTrue(self.check("rule", "equals", "Disk almost full"))
        self.assertTrue(self.check("rule", "not_equals", "Something else"))
        self.assertTrue(self.check("message", "regex", r"\d+%"))
        self.assertFalse(self.check("message", "not_regex", r"\d+%"))

    def test_text_matching_is_case_insensitive(self):
        self.assertTrue(self.check("message", "contains", "/VAR"))
        self.assertTrue(self.check("rule", "equals", "disk ALMOST full"))

    def test_numeric_operators(self):
        self.assertTrue(self.check("metric_value", "gt", "90"))
        self.assertTrue(self.check("metric_value", "gte", "97"))
        self.assertFalse(self.check("metric_value", "lt", "90"))
        self.assertTrue(self.check("flap_count", "gte", "3"))

    def test_severity_compares_by_rank_not_alphabetically(self):
        """'critical' < 'warning' as strings, which would invert the filter."""
        self.assertTrue(self.check("severity", "gte", "warning"))
        self.assertTrue(self.check("severity", "gt", "info"))
        self.assertFalse(self.check("severity", "lt", "warning"))
        self.assertTrue(self.check("severity", "equals", "critical"))

    def test_membership_operators(self):
        self.assertTrue(self.check("severity", "in", "warning, critical"))
        self.assertFalse(self.check("severity", "not_in", "warning, critical"))
        self.assertTrue(self.check("host", "in", "web1,web2"))

    def test_host_tags(self):
        self.assertTrue(self.check("host_tags", "contains", "prod"))
        self.assertFalse(self.check("host_tags", "not_contains", "prod"))
        self.assertTrue(self.check("host_tags", "in", "staging, prod"))
        self.assertTrue(self.check("host_tags", "not_in", "staging, dev"))

    def test_presence_operators(self):
        self.assertTrue(self.check("metric_value", "is_set", ""))
        self.assertFalse(self.check("metric_value", "is_not_set", ""))
        self.alert.metric_value = None
        self.assertTrue(self.check("metric_value", "is_not_set", ""))

    def test_event_name_is_matchable(self):
        self.assertTrue(self.check("event", "equals", "alert_sent"))
        self.assertFalse(self.check("event", "equals", "alert_refired"))

    def test_a_missing_field_never_matches(self):
        """A condition about an alert must not pass on a host-approval event —
        not even a negative one."""
        payload = {"host": self.host, "__event__": "host_approved"}
        for op, value in (("equals", "critical"), ("not_equals", "critical"),
                          ("contains", "disk"), ("not_contains", "disk"),
                          ("gte", "1")):
            self.assertFalse(
                conditions.evaluate_one(
                    {"field": "severity" if "equals" in op else "metric_value",
                     "op": op, "value": value}, payload),
                f"{op} matched on an event with no alert")

    def test_an_invalid_regex_does_not_match(self):
        self.assertFalse(self.check("message", "regex", "([unclosed"))

    def test_comparing_text_as_a_number_does_not_match(self):
        self.assertFalse(self.check("rule", "in", "1,2"))
        self.assertFalse(conditions.evaluate_one(
            {"field": "metric_value", "op": "gt", "value": "abc"}, self.payload))


class ConditionLogicTests(ConditionEvalTests):
    def _auto(self, logic, items):
        return Automation(
            name="a", trigger=Automation.Trigger.EVENT, event="alert_sent",
            condition_logic=logic, conditions=items)

    def test_all_requires_every_condition(self):
        auto = self._auto("all", [
            {"field": "severity", "op": "equals", "value": "critical"},
            {"field": "message", "op": "contains", "value": "/var"},
        ])
        self.assertTrue(conditions.evaluate(auto, self.payload))
        auto.conditions[1]["value"] = "/opt"
        self.assertFalse(conditions.evaluate(auto, self.payload))

    def test_any_needs_only_one(self):
        auto = self._auto("any", [
            {"field": "severity", "op": "equals", "value": "info"},
            {"field": "message", "op": "contains", "value": "/var"},
        ])
        self.assertTrue(conditions.evaluate(auto, self.payload))
        auto.conditions[1]["value"] = "/opt"
        self.assertFalse(conditions.evaluate(auto, self.payload))

    def test_no_conditions_means_no_opinion(self):
        self.assertTrue(conditions.evaluate(self._auto("all", []), self.payload))
        self.assertTrue(conditions.evaluate(self._auto("any", []), self.payload))


class ConditionValidationTests(TestCase):
    def ok(self, items):
        clean, err = conditions.validate(items)
        self.assertEqual(err, "")
        return clean

    def bad(self, items):
        _clean, err = conditions.validate(items)
        self.assertNotEqual(err, "", f"{items!r} was accepted")
        return err

    def test_a_good_condition_round_trips(self):
        clean = self.ok([{"field": "severity", "op": "gte", "value": " warning "}])
        self.assertEqual(clean, [{"field": "severity", "op": "gte",
                                  "value": "warning"}])

    def test_unknown_field_and_operator_are_refused(self):
        self.bad([{"field": "nope", "op": "equals", "value": "x"}])
        self.bad([{"field": "severity", "op": "nope", "value": "x"}])

    def test_an_operator_must_suit_the_field(self):
        """'starts with' on a number is a filter nobody can reason about."""
        self.bad([{"field": "metric_value", "op": "starts_with", "value": "9"}])

    def test_a_bad_severity_word_is_refused(self):
        self.bad([{"field": "severity", "op": "equals", "value": "urgent"}])

    def test_a_non_numeric_number_is_refused(self):
        self.bad([{"field": "metric_value", "op": "gt", "value": "ninety"}])

    def test_a_bad_regex_is_refused_at_save_time(self):
        self.bad([{"field": "message", "op": "regex", "value": "([unclosed"}])

    def test_valueless_operators_need_no_value(self):
        clean = self.ok([{"field": "metric_value", "op": "is_set"}])
        self.assertEqual(clean[0]["value"], "")

    def test_an_empty_value_is_refused(self):
        self.bad([{"field": "message", "op": "contains", "value": "  "}])

    def test_shape_and_size_are_bounded(self):
        self.bad("not a list")
        self.bad(["not an object"])
        self.bad([{"field": "message", "op": "contains", "value": "x"}] * 21)
        self.bad([{"field": "message", "op": "contains", "value": "x" * 501}])

    def test_empty_is_allowed(self):
        self.assertEqual(self.ok([]), [])
        self.assertEqual(self.ok(None), [])


class ConditionEngineTests(TestCase):
    """End to end: an event reaches handle_event and the conditions decide."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="db1", agent_token="t" * 32, tags=["prod"],
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )
        self.rule = AlertRule.objects.create(
            name="Disk almost full", category="disk", metric="disk_percent",
            operator="gt", threshold=90.0, severity=AlertRule.Severity.CRITICAL,
        )
        self.task = TaskDefinition.objects.create(
            name="cleanup", risk_level="low", yaml_source="",
            parsed_spec={"risk": "low",
                         "actions": [{"type": "clear_temp_files", "params": {}}]})

    def _alert(self, severity="critical", message="/var is at 97%", value=97.0):
        return Alert.objects.create(host=self.host, rule=self.rule,
                                    severity=severity, message=message,
                                    metric_value=value)

    def _auto(self, logic, items):
        return Automation.objects.create(
            name="a", enabled=True, trigger=Automation.Trigger.EVENT,
            event="alert_sent", action_kind=Automation.ActionKind.TASK,
            task_definition=self.task, target=Automation.Target.EVENT_HOST,
            condition_logic=logic, conditions=items)

    def _fire(self, alert):
        from apps.tasks.models import Task
        before = Task.objects.count()
        handle_event("alert_sent", {"alert": alert, "host": self.host})
        return Task.objects.count() - before

    def test_conditions_gate_the_run(self):
        self._auto("all", [
            {"field": "metric_value", "op": "gte", "value": "95"},
        ])
        self.assertEqual(self._fire(self._alert(value=97.0)), 1)
        self.assertEqual(self._fire(self._alert(value=91.0)), 0)

    def test_or_runs_when_either_side_holds(self):
        self._auto("any", [
            {"field": "severity", "op": "equals", "value": "critical"},
            {"field": "message", "op": "contains", "value": "backup"},
        ])
        self.assertEqual(self._fire(self._alert(severity="info",
                                               message="backup lagging")), 1)
        self.assertEqual(self._fire(self._alert(severity="critical",
                                               message="/var is at 97%")), 1)
        self.assertEqual(self._fire(self._alert(severity="info",
                                               message="/var is at 97%")), 0)


class ConditionApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        user = get_user_model().objects.create_user("cadmin", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.api.force_authenticate(user)
        self.task = TaskDefinition.objects.create(
            name="cleanup", risk_level="low", yaml_source="",
            parsed_spec={"risk": "low",
                         "actions": [{"type": "clear_temp_files", "params": {}}]})

    def _body(self, **extra):
        body = {"name": "a", "trigger": "event", "event": "alert_sent",
                "action_kind": "task", "task_definition": str(self.task.id),
                "target": "event_host"}
        body.update(extra)
        return body

    def test_meta_advertises_fields_and_operators(self):
        body = self.api.get("/api/v1/automations/").json()
        meta = body["condition_meta"]
        names = [f["name"] for f in meta["fields"]]
        self.assertIn("severity", names)
        self.assertIn("metric_value", names)
        severity = next(f for f in meta["fields"] if f["name"] == "severity")
        self.assertIn("gte", severity["operators"])
        self.assertNotIn("starts_with", severity["operators"])

    def test_conditions_save_and_come_back(self):
        resp = self.api.post("/api/v1/automations/", self._body(
            condition_logic="any",
            conditions=[{"field": "severity", "op": "gte", "value": "warning"}],
        ), format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["condition_logic"], "any")
        self.assertEqual(resp.json()["conditions"][0]["op"], "gte")

    def test_a_bad_condition_is_a_400_that_says_why(self):
        resp = self.api.post("/api/v1/automations/", self._body(
            conditions=[{"field": "metric_value", "op": "gt", "value": "lots"}],
        ), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a number", resp.json()["detail"])

    def test_a_bad_logic_value_is_refused(self):
        resp = self.api.post("/api/v1/automations/",
                             self._body(condition_logic="maybe"), format="json")
        self.assertEqual(resp.status_code, 400)


class ConditionYamlTests(TestCase):
    def test_conditions_round_trip_through_yaml(self):
        from apps.automations import community_yaml

        parsed = community_yaml.parse(
            "name: A\n"
            "trigger: event\n"
            "event: alert_sent\n"
            "condition_logic: any\n"
            "conditions:\n"
            "  - field: severity\n"
            "    op: gte\n"
            "    value: warning\n"
            "  - field: message\n"
            "    op: contains\n"
            "    value: /var\n"
            "action_kind: task\n"
            "task: cleanup\n"
            "target: event_host\n"
        )
        self.assertEqual(parsed["condition_logic"], "any")
        self.assertEqual(len(parsed["conditions"]), 2)
        self.assertEqual(parsed["conditions"][0]["field"], "severity")

    def test_a_bad_condition_in_yaml_is_rejected(self):
        from apps.automations import community_yaml
        from vigil.contentyaml import ContentYamlError

        with self.assertRaises(ContentYamlError):
            community_yaml.parse(
                "name: A\ntrigger: event\nevent: alert_sent\n"
                "conditions:\n  - field: nope\n    op: equals\n    value: x\n"
                "action_kind: task\ntask: cleanup\ntarget: event_host\n")
