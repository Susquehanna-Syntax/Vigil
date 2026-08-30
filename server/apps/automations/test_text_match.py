"""Text matching on automations, across every event type and every operator.

The bug these were written for: `text_ok` only ever read an alert's rule name
and message. Every other event — insight_created, task_completed, the rebuild
events — carries no `alert`, so the filter was silently ignored and the
automation fired regardless of what the operator had typed. The editor
compounded it by hiding the filter for those events entirely.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

from .engine import event_text, text_ok
from .models import Automation

YAML = "name: T\nrisk: low\nactions:\n  - type: clear_temp_files\n"


class _Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tm", password="x")
        self.host = Host.objects.create(hostname="tm1", ip_address="10.41.0.1",
                                        agent_token="tm1")
        self.definition = TaskDefinition.objects.create(
            name="T", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")

    def auto(self, **kw):
        kw.setdefault("event", "alert_fired")
        return Automation.objects.create(
            name=f"A{Automation.objects.count()}",
            trigger=Automation.Trigger.EVENT,
            action_kind=Automation.ActionKind.TASK,
            task_definition=self.definition, created_by=self.user, **kw)

    def alert(self, rule_name="Disk low", message="/var is at 91%"):
        rule = AlertRule.objects.create(name=rule_name, metric="disk",
                                        threshold=90, severity="warning")
        return Alert.objects.create(host=self.host, rule=rule,
                                    message=message, severity="warning")


class EventTextExtractionTests(_Base):
    """Every event carries something nameable; the filter must find it."""

    def test_alert_gives_rule_name_and_message(self):
        name, message = event_text({"alert": self.alert()})
        self.assertEqual(name, "Disk low")
        self.assertEqual(message, "/var is at 91%")

    def test_host_event_gives_the_hostname(self):
        name, _ = event_text({"host": self.host})
        self.assertEqual(name, "tm1")

    def test_empty_payload_gives_empty_strings(self):
        self.assertEqual(event_text({}), ("", ""))

    def test_unknown_payload_does_not_raise(self):
        self.assertEqual(event_text({"something_else": object()}), ("", ""))


class NonAlertEventTests(_Base):
    """The actual bug: a filter on a non-alert event used to be ignored."""

    def test_filter_on_a_non_alert_event_is_no_longer_ignored(self):
        auto = self.auto(event="task_completed", match_text="nomatch",
                         match_mode="contains", match_field="any")
        # A host-carrying payload whose text does not contain the needle.
        self.assertFalse(text_ok(auto, None, {"host": self.host}),
                         "the filter must be applied, not silently passed")

    def test_filter_on_a_non_alert_event_matches_when_it_should(self):
        auto = self.auto(event="host_approved", match_text="tm1",
                         match_mode="contains", match_field="any")
        self.assertTrue(text_ok(auto, None, {"host": self.host}))

    def test_an_empty_filter_still_matches_everything(self):
        auto = self.auto(event="task_completed", match_text="")
        self.assertTrue(text_ok(auto, None, {"host": self.host}))

    def test_a_filter_against_a_payload_with_no_text_does_not_match(self):
        """No text to match against is 'no match', not 'matches everything'.
        The old behaviour let the automation fire for anything."""
        auto = self.auto(match_text="anything", match_mode="contains")
        self.assertFalse(text_ok(auto, None, {}))


class OperatorTests(_Base):
    def _check(self, mode, needle, expected, field="any"):
        auto = self.auto(match_text=needle, match_mode=mode, match_field=field)
        self.assertEqual(text_ok(auto, self.alert()), expected,
                         f"{mode} {needle!r}")

    def test_contains(self):
        self._check("contains", "/var", True)
        self._check("contains", "nope", False)

    def test_not_contains(self):
        self._check("not_contains", "nope", True)
        self._check("not_contains", "/var", False)

    def test_not_contains_is_true_only_when_no_field_matches(self):
        """A filter meant to exclude must not let something through because it
        matched the field the operator was not thinking about."""
        self._check("not_contains", "disk", False)   # in the rule name

    def test_equals(self):
        self._check("equals", "disk low", True, field="rule")
        self._check("equals", "disk", False, field="rule")

    def test_not_equals(self):
        self._check("not_equals", "something else", True, field="rule")

    def test_starts_with(self):
        self._check("starts_with", "disk", True, field="rule")
        self._check("starts_with", "low", False, field="rule")

    def test_ends_with(self):
        self._check("ends_with", "low", True, field="rule")
        self._check("ends_with", "disk", False, field="rule")

    def test_regex(self):
        self._check("regex", r"\d+%", True, field="message")
        self._check("regex", r"^\d+$", False, field="message")

    def test_not_regex(self):
        self._check("not_regex", r"^\d+$", True, field="message")

    def test_an_invalid_regex_does_not_match_anything(self):
        """A malformed pattern must not fire the automation for everything —
        'I could not evaluate this filter' is not 'this filter passed'."""
        auto = self.auto(match_text="([unclosed", match_mode="regex")
        self.assertFalse(text_ok(auto, self.alert()))

    def test_matching_is_case_insensitive(self):
        self._check("contains", "DISK", True)
        self._check("equals", "DISK LOW", True, field="rule")


class FieldScopeTests(_Base):
    def test_rule_scope_ignores_the_message(self):
        auto = self.auto(match_text="/var", match_mode="contains",
                         match_field="rule")
        self.assertFalse(text_ok(auto, self.alert()))

    def test_message_scope_ignores_the_rule_name(self):
        auto = self.auto(match_text="disk", match_mode="contains",
                         match_field="message")
        self.assertFalse(text_ok(auto, self.alert()))

    def test_any_scope_reads_both(self):
        for needle in ("disk", "/var"):
            with self.subTest(needle=needle):
                auto = self.auto(match_text=needle, match_mode="contains",
                                 match_field="any")
                self.assertTrue(text_ok(auto, self.alert()))


class DispatchTests(_Base):
    """End to end through the real event handler."""

    def test_a_matching_filter_fires(self):
        from apps.tasks.models import Task

        from .engine import handle_event

        self.host.mode = Host.Mode.MANAGED
        self.host.save()
        self.auto(match_text="/var", match_mode="contains")
        before = Task.objects.count()
        handle_event("alert_fired", {"alert": self.alert(), "host": self.host})
        self.assertEqual(Task.objects.count() - before, 1)

    def test_a_non_matching_filter_does_not_fire(self):
        from apps.tasks.models import Task

        from .engine import handle_event

        self.host.mode = Host.Mode.MANAGED
        self.host.save()
        self.auto(match_text="nothing-like-this", match_mode="contains")
        before = Task.objects.count()
        handle_event("alert_fired", {"alert": self.alert(), "host": self.host})
        self.assertEqual(Task.objects.count() - before, 0)

    def test_a_filter_on_a_host_event_is_honoured(self):
        """The regression: this event has no alert, so the filter used to be
        ignored and the automation fired regardless."""
        from apps.tasks.models import Task

        from .engine import handle_event

        self.host.mode = Host.Mode.MANAGED
        self.host.save()
        self.auto(event="host_approved", match_text="not-this-host",
                  match_mode="contains", target=Automation.Target.EVENT_HOST)
        before = Task.objects.count()
        handle_event("host_approved", {"host": self.host})
        self.assertEqual(Task.objects.count() - before, 0,
                         "a non-matching filter must stop a non-alert event too")
