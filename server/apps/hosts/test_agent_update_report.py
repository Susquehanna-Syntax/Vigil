"""An agent reports how its self-update ended (sprint M8, the agent's
update_guard). The report is the agent's own claim: stored for people to see,
cleaned on the way in, and never something a host's permissions hang on."""
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.tasks.models import PatchRollout, PatchWave, Task, TaskDefinition, TaskRun
from apps.tasks.rollout import _evaluate_locked
from apps.tasks.spec import parse_and_validate

TOKEN = "u" * 32

ROLLBACK_PREFIX = "Agent update rolled back:"

UPDATE_YAML = (
    "name: Update agent\nrisk: low\nactions:\n"
    "  - type: run_command\n    params:\n"
    "      command: echo update\n"
)


def make_update_task(host, *, run=None, completed_ago=timedelta(minutes=5), state=Task.State.COMPLETED):
    return Task.objects.create(
        host=host, run=run, action="update_agent", params={}, state=state,
        nonce=secrets.token_hex(32), completed_at=now() - completed_ago,
    )


def rolled_back(reason="3 check-ins failed after the update: SSLError"):
    return {"state": "rolled_back", "from_version": "2026.16.0", "to_version": "2026.17.0",
            "running": "2026.16.0", "reason": reason}


class AgentUpdateReportTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="web-01", agent_token=TOKEN,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        self.agent = APIClient()
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def checkin(self, **extra):
        resp = self.agent.post("/api/v1/checkin", {"metrics": [], **extra}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.host.refresh_from_db()
        return self.host.agent_update

    def test_an_ordinary_checkin_reports_nothing(self):
        self.assertIsNone(self.checkin())

    def test_a_confirmed_update_is_stored_with_when_it_arrived(self):
        stored = self.checkin(agent_update={
            "state": "confirmed", "from_version": "2026.16.0", "to_version": "2026.17.0",
            "running": "2026.17.0", "reason": ""})
        self.assertEqual({k: stored[k] for k in ("state", "from_version", "to_version", "running")},
                         {"state": "confirmed", "from_version": "2026.16.0",
                          "to_version": "2026.17.0", "running": "2026.17.0"})
        self.assertTrue(stored["at"])

    def test_a_rollback_keeps_its_reason_and_is_logged(self):
        with self.assertLogs("apps.hosts", level="WARNING") as logs:
            stored = self.checkin(agent_update={
                "state": "rolled_back", "from_version": "2026.16.0", "to_version": "2026.17.0",
                "running": "2026.16.0",
                "reason": "3 check-ins failed after the update: SSLError"})
        self.assertEqual(stored["state"], "rolled_back")
        self.assertIn("SSLError", stored["reason"])
        self.assertTrue(any("rolled_back" in line and "web-01" in line for line in logs.output))

    def test_a_later_ordinary_checkin_does_not_erase_a_rollback(self):
        self.checkin(agent_update={"state": "rolled_back", "from_version": "a",
                                   "to_version": "b", "running": "a", "reason": "x"})
        self.assertEqual(self.checkin()["state"], "rolled_back")

    def test_what_is_not_an_outcome_is_ignored(self):
        for bad in ("rolled_back", ["rolled_back"], {}, {"state": "pending"},
                    {"state": "owned"}, {"state": None}, 7):
            with self.subTest(bad=bad):
                self.assertIsNone(self.checkin(agent_update=bad))

    def test_the_agents_text_is_cut_and_flattened(self):
        stored = self.checkin(agent_update={
            "state": "not_applied", "from_version": "v" * 500, "to_version": 9,
            "running": {"x": 1}, "reason": "line one\nline two " + "z" * 5000,
            "at": "1999-01-01T00:00:00Z", "extra": "<script>"})
        self.assertEqual(len(stored["from_version"]), 50)
        self.assertEqual(stored["to_version"], "")
        self.assertEqual(stored["running"], "")
        self.assertLessEqual(len(stored["reason"]), 300)
        self.assertNotIn("\n", stored["reason"])
        self.assertNotIn("extra", stored)
        self.assertFalse(stored["at"].startswith("1999"), "the server's clock, not the agent's")

    def test_a_rollback_fails_the_recent_update_task_and_its_run(self):
        run = TaskRun.objects.create(name_snapshot="Update agent", host_count=1, step_count=1,
                                     state=TaskRun.State.COMPLETED, finished_at=now())
        task = make_update_task(self.host, run=run)
        self.checkin(agent_update=rolled_back("3 check-ins failed after the update: SSLError"))
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertIn("Rolled back on the host: 3 check-ins failed after the update: SSLError",
                      task.result_output)
        self.assertEqual(run.state, TaskRun.State.FAILED)

    def test_a_not_applied_update_fails_the_task_with_its_own_wording(self):
        task = make_update_task(self.host)
        self.checkin(agent_update={"state": "not_applied", "from_version": "2026.16.0",
                                   "to_version": "2026.17.0", "running": "2026.16.0",
                                   "reason": "the new binary is still the old one"})
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertEqual(task.result_output,
                         "The update did not take effect: the new binary is still the old one")
        self.assertFalse(Alert.objects.filter(message__startswith=ROLLBACK_PREFIX).exists())

    def test_a_confirmed_update_changes_no_task(self):
        run = TaskRun.objects.create(name_snapshot="Update agent", host_count=1, step_count=1,
                                     state=TaskRun.State.COMPLETED, finished_at=now())
        task = make_update_task(self.host, run=run)
        self.checkin(agent_update={"state": "confirmed", "from_version": "2026.16.0",
                                   "to_version": "2026.17.0", "running": "2026.17.0",
                                   "reason": ""})
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.state, Task.State.COMPLETED)
        self.assertEqual(task.result_output, "")
        self.assertEqual(run.state, TaskRun.State.COMPLETED)

    def test_an_update_completed_three_days_ago_is_left_alone(self):
        task = make_update_task(self.host, completed_ago=timedelta(days=3))
        self.checkin(agent_update=rolled_back())
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.COMPLETED)
        self.assertEqual(task.result_output, "")

    def test_a_repeated_rollback_report_changes_nothing_more(self):
        task = make_update_task(self.host)
        self.checkin(agent_update=rolled_back("boom"))
        self.checkin(agent_update=rolled_back("boom"))
        task.refresh_from_db()
        self.assertEqual(task.result_output, "Rolled back on the host: boom")
        self.assertEqual(Alert.objects.filter(message__startswith=ROLLBACK_PREFIX).count(), 1)

    def test_a_rollback_raises_one_alert_and_a_confirmed_update_resolves_it(self):
        self.checkin(agent_update=rolled_back("boom"))
        self.checkin(agent_update=rolled_back("boom"))
        alerts = Alert.objects.filter(message__startswith=ROLLBACK_PREFIX)
        self.assertEqual(alerts.count(), 1)
        alert = alerts.get()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertEqual(alert.severity, "warning")
        self.assertIsNone(alert.rule)
        self.assertEqual(alert.fix_context["hostname"], "web-01")
        self.assertIn("returned to v2026.16.0 from v2026.17.0: boom", alert.message)

        self.checkin(agent_update={"state": "confirmed", "from_version": "2026.16.0",
                                   "to_version": "2026.17.0", "running": "2026.17.0",
                                   "reason": ""})
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.RESOLVED)
        self.assertIsNotNone(alert.resolved_at)

    def test_the_host_api_shows_it(self):
        self.checkin(agent_update={"state": "rolled_back", "from_version": "2026.16.0",
                                   "to_version": "2026.17.0", "running": "2026.16.0",
                                   "reason": "check-ins failed"})
        admin = get_user_model().objects.create_user("admin", password="x")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        client = APIClient()
        client.force_authenticate(admin)
        row = next(h for h in client.get("/api/v1/hosts/").json() if h["hostname"] == "web-01")
        self.assertEqual(row["agent_update"]["state"], "rolled_back")


class AgentUpdateApprovalTests(TestCase):
    """A host waiting for approval can say how its update went, but its token
    cannot fail a task or raise an alert until an admin approves it."""

    def test_a_pending_host_records_the_outcome_and_nothing_else(self):
        from apps.alerts.models import Alert
        host = Host.objects.create(hostname="new-01", agent_token="p" * 32,
                                   status=Host.Status.PENDING, mode=Host.Mode.MANAGED)
        agent = APIClient()
        agent.credentials(HTTP_AUTHORIZATION=f"Bearer {'p' * 32}")
        agent.post("/api/v1/checkin", {"metrics": [], "agent_update": {
            "state": "rolled_back", "from_version": "a", "to_version": "b",
            "running": "a", "reason": "x"}}, format="json")
        host.refresh_from_db()
        self.assertEqual(host.agent_update["state"], "rolled_back")
        self.assertFalse(Alert.objects.filter(host=host).exists())


class AgentUpdateRolloutGateTests(TestCase):
    """A bad agent release rolled out in waves halts at the failure gate."""

    def test_rollbacks_from_a_whole_wave_halt_the_rollout(self):
        user = get_user_model().objects.create_user("op", password="pw")
        definition = TaskDefinition.objects.create(
            owner=user, name="Update agent", yaml_source=UPDATE_YAML,
            parsed_spec=parse_and_validate(UPDATE_YAML))
        wave = PatchWave.objects.create(name="Canary", order=1, tags=[], validation_hours=0)
        rollout = PatchRollout.objects.create(
            definition=definition, state=PatchRollout.State.RUNNING, current_wave=wave,
            started_at=now(), wave_started_at=now(), min_results_before_halt=3,
            failure_threshold_pct=10)
        run = TaskRun.objects.create(rollout=rollout, wave=wave, host_count=3, step_count=1,
                                     state=TaskRun.State.COMPLETED, finished_at=now())
        fleet = []
        for i in range(1, 4):
            token = f"fleettok{i}0123456789"
            host = Host.objects.create(hostname=f"fleet-{i}", agent_token=token,
                                       status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
            make_update_task(host, run=run)
            fleet.append(token)

        for token in fleet:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            resp = client.post("/api/v1/checkin", {"metrics": [], "agent_update": rolled_back()},
                               format="json")
            self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(Task.objects.filter(run=run, state=Task.State.FAILED).count(), 3)
        _evaluate_locked(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.HALTED)
        self.assertIn("3/3", rollout.halted_reason)
