"""``raise_alert`` / ``resolve_alert`` / ``send_notification`` on the server side.

These are marker actions: the agent reports the step and the server does the
work when the result arrives. What that work is comes from the signed task —
severity, key and channel — and only the message comes from the agent's report.
These tests are the guard on that split.
"""
import uuid
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.alerts.models import Alert, AlertRule, NotificationChannel
from apps.alerts.task_actions import (
    ACTIONS,
    apply,
    fired_steps,
    reported_text,
)
from apps.hosts.models import Host
from apps.tasks.models import Task

_NOTIF = "apps.alerts.task_actions.dispatch_alert_notification"


def _script_task(host, steps, reports, state=Task.State.COMPLETED):
    """A multi-step task whose signed steps are *steps* and whose agent report
    is *reports*, as the result endpoint would have stored both."""
    return Task.objects.create(
        host=host, action="_script", nonce=uuid.uuid4().hex,
        params={"steps": steps}, result_data={"steps": reports}, state=state)


def _raise_step(step_id="a", severity="warning", message="signed text", key=None):
    params = {"severity": severity, "message": message}
    if key is not None:
        params["key"] = key
    return {"id": step_id, "action": "raise_alert", "params": params}


def _ok(step_id, **result):
    return {"id": step_id, "status": "ok", "result": result}


class FiredStepsTests(TestCase):
    """The seam QA-39's Jackil actions reuse: which signed steps fired, and
    which text the server is allowed to take from the agent."""

    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32)

    def test_only_a_step_the_agent_reported_ok_fires(self):
        task = _script_task(
            self.host,
            [_raise_step("a"), _raise_step("b"), _raise_step("c")],
            [_ok("a"), {"id": "b", "status": "skipped", "result": {}}])
        fired = fired_steps(task, ACTIONS)
        self.assertEqual([signed["id"] for signed, _ in fired], ["a"])

    def test_a_signed_step_with_no_report_does_not_fire(self):
        task = _script_task(self.host, [_raise_step("a")], [])
        self.assertEqual(fired_steps(task, ACTIONS), [])

    def test_a_report_without_a_signed_step_is_ignored(self):
        """The report is untrusted: nothing it names can create an alert the
        signed task did not ask for."""
        task = _script_task(self.host, [{"id": "a", "action": "check_service",
                                         "params": {}}], [_ok("a")])
        self.assertEqual(fired_steps(task, ACTIONS), [])

    def test_a_step_of_another_action_never_fires(self):
        """An action outside *actions* is not this caller's business, even when
        the agent reported it ok."""
        task = _script_task(
            self.host,
            [{"id": "a", "action": "add_tag", "params": {"tags": "role:web"}},
             _raise_step("b")],
            [_ok("a"), _ok("b")])
        fired = fired_steps(task, ("raise_alert",))
        self.assertEqual([signed["id"] for signed, _ in fired], ["b"])
        self.assertEqual([signed["id"] for signed, _ in fired_steps(task, ("add_tag",))],
                         ["a"])

    def test_a_legacy_single_action_task_fires_nothing(self):
        """No signed step id means no report can be matched to it, so these
        actions stay silent rather than acting on an unverifiable report."""
        task = Task.objects.create(
            host=self.host, action="raise_alert", nonce=uuid.uuid4().hex,
            params={"severity": "critical", "message": "m"},
            result_data={"steps": [_ok("step1")]}, state=Task.State.COMPLETED)
        self.assertEqual(fired_steps(task, ACTIONS), [])

    def test_reported_text_prefers_the_report_and_falls_back(self):
        signed = {"message": "signed"}
        self.assertEqual(reported_text(signed, {"message": " rendered "}, "message", 100),
                         "rendered")
        self.assertEqual(reported_text(signed, {"message": "   "}, "message", 100), "signed")
        self.assertEqual(reported_text(signed, {}, "message", 100), "signed")
        self.assertEqual(reported_text(signed, {"message": 7}, "message", 100), "signed")
        self.assertEqual(reported_text(signed, {"message": "abcdef"}, "message", 3), "abc")


class RaiseAlertTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32)

    def _apply(self, step, result_message="reported text"):
        task = _script_task(self.host, [step], [_ok(step["id"], message=result_message)])
        apply(task)

    @mock.patch(_NOTIF)
    def test_raise_alert_creates_and_notifies(self, notify):
        self._apply(_raise_step(severity="critical"))
        alert = Alert.objects.get()
        self.assertIsNone(alert.rule)
        self.assertEqual(alert.severity, AlertRule.Severity.CRITICAL)
        self.assertEqual(alert.message, "reported text")
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args[0][1], "firing")

    @mock.patch(_NOTIF)
    def test_raise_alert_is_deduplicated_by_key(self, notify):
        step = _raise_step(key="disk-cleanup")
        self._apply(step)
        self._apply(step)
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(notify.call_count, 1)

    @mock.patch(_NOTIF)
    def test_a_keyless_alert_is_deduplicated_by_message(self, notify):
        step = _raise_step(key=None)
        self._apply(step)
        self._apply(step)
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(notify.call_count, 1)

    @mock.patch(_NOTIF)
    def test_a_resolved_alert_does_not_suppress_a_new_one(self, notify):
        step = _raise_step(key="disk-cleanup")
        self._apply(step)
        Alert.objects.update(state=Alert.State.RESOLVED)
        self._apply(step)
        self.assertEqual(Alert.objects.count(), 2)
        self.assertEqual(notify.call_count, 2)

    @mock.patch(_NOTIF)
    def test_raise_alert_ignores_agent_severity(self, notify):
        """The whole trust argument: the report's severity is decoration."""
        task = _script_task(self.host, [_raise_step(severity="critical", key="k")],
                            [{"id": "a", "status": "ok",
                              "result": {"message": "m", "severity": "info"}}])
        apply(task)
        self.assertEqual(Alert.objects.get().severity, AlertRule.Severity.CRITICAL)

    @mock.patch(_NOTIF)
    def test_message_is_capped(self, notify):
        self._apply(_raise_step(severity="info"), result_message="x" * 5000)
        self.assertEqual(len(Alert.objects.get().message), 2000)

    @mock.patch(_NOTIF)
    def test_skipped_step_does_not_fire(self, notify):
        step = _raise_step()
        task = _script_task(self.host, [step],
                            [{"id": step["id"], "status": "skipped", "result": {}}])
        apply(task)
        self.assertEqual(Alert.objects.count(), 0)
        notify.assert_not_called()

    @mock.patch(_NOTIF)
    def test_an_unknown_signed_severity_is_skipped(self, notify):
        self._apply(_raise_step(severity="loud"))
        self.assertEqual(Alert.objects.count(), 0)
        notify.assert_not_called()

    @mock.patch(_NOTIF)
    def test_a_failing_step_does_not_stop_the_next(self, notify):
        """Each step is guarded on its own."""
        good = _raise_step("b", severity="info", key="ok-key")
        task = _script_task(self.host, [_raise_step("a", severity="loud"), good],
                            [_ok("a"), _ok("b")])
        apply(task)
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(Alert.objects.get().source_key, "ok-key")

    @mock.patch(_NOTIF)
    def test_the_signed_message_is_used_when_the_agent_reported_none(self, notify):
        step = _raise_step(message="signed text")
        task = _script_task(self.host, [step], [_ok(step["id"])])
        apply(task)
        self.assertEqual(Alert.objects.get().message, "signed text")


class ResolveAlertTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32)
        self.other = Host.objects.create(hostname="other", agent_token="u" * 32)
        self.alert = Alert.objects.create(
            host=self.host, rule=None, severity=AlertRule.Severity.WARNING,
            message="disk full", source_key="disk-cleanup")
        self.twin = Alert.objects.create(
            host=self.other, rule=None, severity=AlertRule.Severity.WARNING,
            message="disk full", source_key="disk-cleanup")

    @mock.patch(_NOTIF)
    def test_resolve_alert_by_key(self, notify):
        task = _script_task(self.host,
                            [{"id": "r", "action": "resolve_alert",
                              "params": {"key": "disk-cleanup"}}],
                            [_ok("r", key="disk-cleanup")])
        apply(task)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, Alert.State.RESOLVED)
        self.assertIsNotNone(self.alert.resolved_at)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args[0][1], "resolved")
        self.twin.refresh_from_db()
        self.assertEqual(self.twin.state, Alert.State.FIRING)

    @mock.patch(_NOTIF)
    def test_an_acknowledged_alert_resolves_too(self, notify):
        Alert.objects.filter(pk=self.alert.pk).update(state=Alert.State.ACKNOWLEDGED)
        task = _script_task(self.host,
                            [{"id": "r", "action": "resolve_alert",
                              "params": {"key": "disk-cleanup"}}],
                            [_ok("r")])
        apply(task)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, Alert.State.RESOLVED)

    @mock.patch(_NOTIF)
    def test_an_empty_key_resolves_nothing(self, notify):
        task = _script_task(self.host,
                            [{"id": "r", "action": "resolve_alert", "params": {"key": " "}}],
                            [_ok("r")])
        apply(task)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, Alert.State.FIRING)
        notify.assert_not_called()


class SendNotificationPayloadTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32)
        # Patched where task_actions looks them up: _DISPATCHERS holds the
        # function objects themselves, so patching the module attribute after
        # import would not reach them.
        self.webhook = mock.Mock()
        self.email = mock.Mock()
        self.dispatcher = mock.patch.dict(
            "apps.alerts.task_actions._DISPATCHERS",
            {NotificationChannel.Kind.WEBHOOK: self.webhook,
             NotificationChannel.Kind.EMAIL: self.email})
        self.dispatcher.start()
        self.addCleanup(self.dispatcher.stop)
        self.ops = NotificationChannel.objects.create(
            name="Ops", kind=NotificationChannel.Kind.WEBHOOK,
            config={"url": "https://example.invalid/hook"})
        self.email_channel = NotificationChannel.objects.create(
            name="Ops email", kind=NotificationChannel.Kind.EMAIL,
            config={"recipients": ["ops@example.invalid"]})

    def _task(self, params, message):
        return _script_task(self.host,
                            [{"id": "n", "action": "send_notification", "params": params}],
                            [_ok("n", message=message)])

    def test_send_notification_named_channel_only(self):
        apply(self._task({"message": "hi", "channel": "Ops"}, "hi"))
        self.assertEqual(self.webhook.call_count, 1)
        self.assertEqual(self.webhook.call_args[0][0].name, "Ops")
        self.email.assert_not_called()
        self.assertEqual(Alert.objects.count(), 0)

    def test_without_a_channel_every_enabled_channel_gets_it(self):
        apply(self._task({"message": "hi"}, "hi"))
        self.assertEqual(self.webhook.call_count, 1)
        self.assertEqual(self.email.call_count, 1)

    def test_the_payload_has_every_key_a_sender_indexes(self):
        apply(self._task({"message": "hi", "channel": "Ops"}, "hi"))
        payload = self.webhook.call_args[0][1]
        self.assertEqual(sorted(payload),
                         ["alert_id", "event", "fired_at", "host", "host_id",
                          "message", "metric_value", "resolved_at", "rule",
                          "severity"])
        self.assertEqual(payload["event"], "notification")
        self.assertIsNone(payload["alert_id"])
        self.assertEqual(payload["host"], "h")
        self.assertEqual(payload["severity"], AlertRule.Severity.INFO)
        self.assertIsNone(payload["rule"])
        self.assertTrue(payload["fired_at"])
        self.assertIsNone(payload["resolved_at"])

    def test_a_channel_named_but_absent_sends_nothing(self):
        apply(self._task({"message": "hi", "channel": "Nope"}, "hi"))
        self.webhook.assert_not_called()
        self.email.assert_not_called()

    def test_a_disabled_channel_is_skipped(self):
        NotificationChannel.objects.filter(name="Ops").update(enabled=False)
        apply(self._task({"message": "hi"}, "hi"))
        self.webhook.assert_not_called()
        self.assertEqual(self.email.call_count, 1)


class ResultEndpointTests(TestCase):
    """The actions run from the result endpoint, not only from a test call."""

    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="tok-result",
                                        status=Host.Status.ONLINE)

    def _post(self, action, params, status="ok", state="completed"):
        task = Task.objects.create(
            host=self.host, action="_script", nonce=uuid.uuid4().hex,
            params={"steps": [{"id": "a", "action": action, "params": params}]},
            state=Task.State.DISPATCHED)
        client = APIClient()
        response = client.post(
            "/api/v1/tasks/result/",
            data={"task_id": str(task.id), "state": state, "output": "done",
                  "steps": [{"id": "a", "status": status,
                             "result": {"message": "reported", "key": "k"}}]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")
        self.assertEqual(response.status_code, 200, response.content)
        return task

    @mock.patch(_NOTIF)
    def test_result_endpoint_runs_actions(self, notify):
        self._post("raise_alert", {"severity": "critical", "message": "signed",
                                   "key": "k"})
        alert = Alert.objects.get()
        self.assertEqual(alert.severity, AlertRule.Severity.CRITICAL)
        self.assertEqual(alert.message, "reported")
        self.assertEqual(notify.call_count, 1)

    @mock.patch(_NOTIF)
    def test_a_failed_task_still_raises_what_ran(self, notify):
        """A later step failing must not swallow the alert an earlier one asked
        for — which is why the hook runs on FAILED too."""
        self._post("raise_alert", {"severity": "info", "message": "signed"},
                   state="failed")
        self.assertEqual(Alert.objects.count(), 1)

    @mock.patch(_NOTIF)
    def test_a_skipped_step_does_not_raise_from_the_endpoint(self, notify):
        self._post("raise_alert", {"severity": "info", "message": "signed"},
                   status="skipped")
        self.assertEqual(Alert.objects.count(), 0)
