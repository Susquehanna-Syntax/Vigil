"""Hunts stay open: how long a hunt waits for hosts that never check in.

`stays_open` is a per-hunt-step param (default 7 days). Deploy sets
`Task.expires_at` from the largest `stays_open`; check-in stops dispatching a
pending task past that time; the sweep marks it `expired` with a
"did not report" output; the results API shows it as `did_not_report`.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import (
    DEFAULT_STAYS_OPEN,
    SpecError,
    hunt_expiry,
    parse_and_validate,
    stays_open_seconds,
)

now = timezone.now

HUNT_YAML = (
    "name: Hunt jars\n"
    "risk: low\n"
    "actions:\n"
    "  - id: jars\n"
    "    type: hunt_file\n"
    "    params:\n"
    "      name: '*.jar'\n"
)

HUNT_PARAMS = {"steps": [{"id": "jars", "action": "hunt_file", "params": {}}]}


def _host(n):
    return Host.objects.create(
        hostname=f"h{n}",
        agent_token=("t" * 31) + str(n),
        status=Host.Status.ONLINE,
        mode=Host.Mode.MANAGED,
    )


def _hunt_yaml_with(stays_open_value, step_id="jars"):
    param_line = ""
    if stays_open_value is not None:
        param_line = f"\n      stays_open: {stays_open_value}"
    return (
        "name: Hunt jars\n"
        "risk: low\n"
        "actions:\n"
        f"  - id: {step_id}\n"
        "    type: hunt_file\n"
        "    params:\n"
        f"      name: '*.jar'{param_line}\n"
    )


class StaysOpenParamTests(TestCase):
    def test_stays_open_formats(self):
        self.assertEqual(stays_open_seconds("2h"), 7200)
        self.assertEqual(stays_open_seconds("1d"), 86400)
        self.assertEqual(stays_open_seconds(90000), 90000)

        spec = parse_and_validate(_hunt_yaml_with("'2h'"))
        self.assertEqual(spec["actions"][0]["params"]["stays_open"], "2h")
        spec = parse_and_validate(_hunt_yaml_with("2h"))
        self.assertEqual(spec["actions"][0]["params"]["stays_open"], "2h")

    def test_stays_open_bounds_refused(self):
        for value in ("59m", "0m", "31d", "2h - 60", "soon", "1h30m", True, 1.5, None):
            with self.assertRaises(SpecError, msg=f"{value!r} should be refused"):
                stays_open_seconds(value)
        self.assertEqual(stays_open_seconds("1h"), 3600)
        self.assertEqual(stays_open_seconds("30d"), 30 * 86400)

        with self.assertRaises(SpecError):
            parse_and_validate(_hunt_yaml_with("'10m'"))
        with self.assertRaises(SpecError):
            parse_and_validate(_hunt_yaml_with("'40d'"))

    def test_hunt_deploy_sets_expiry_default_7d(self):
        user = get_user_model().objects.create_user("op", password="pw")
        self.client = APIClient()
        self.client.force_login(user)
        host = _host(1)
        d = TaskDefinition.objects.create(
            owner=user, name="Hunt jars", yaml_source=HUNT_YAML,
            parsed_spec=parse_and_validate(HUNT_YAML),
        )
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {"host_ids": [str(host.id)], "totp": "123456"},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)

        task = Task.objects.get(host=host)
        self.assertEqual(task.state, Task.State.PENDING)
        self.assertIsNotNone(task.expires_at)
        delta = (task.expires_at - now()).total_seconds()
        self.assertGreater(delta, DEFAULT_STAYS_OPEN - 30)
        self.assertLessEqual(delta, DEFAULT_STAYS_OPEN)

    def test_largest_stays_open_wins(self):
        yaml_source = (
            "name: Hunt two\n"
            "risk: low\n"
            "actions:\n"
            "  - id: short\n"
            "    type: hunt_file\n"
            "    params:\n"
            "      name: 'a'\n"
            "      stays_open: '2d'\n"
            "  - id: long\n"
            "    type: hunt_file\n"
            "    params:\n"
            "      name: 'b'\n"
            "      stays_open: '20d'\n"
        )
        parsed = parse_and_validate(yaml_source)
        t = now()
        expiry = hunt_expiry(parsed, t)
        self.assertEqual(expiry, t + timedelta(days=20))

        # The deployed-steps shape (params["steps"]) behaves the same.
        steps = [
            {"id": "short", "action": "hunt_file", "params": {"stays_open": "2d"}},
            {"id": "long", "action": "hunt_file", "params": {"stays_open": "20d"}},
        ]
        self.assertEqual(hunt_expiry(steps, t), t + timedelta(days=20))

    def test_non_hunt_tasks_never_expire_pending(self):
        t = now()
        self.assertIsNone(hunt_expiry([], t))
        self.assertIsNone(hunt_expiry(
            [{"id": "svc", "action": "check_service", "params": {}}], t))
        self.assertIsNone(hunt_expiry(
            {"actions": [{"id": "svc", "type": "check_service", "params": {}}]}, t))
        # A hunt step without stays_open still gets the default.
        self.assertEqual(hunt_expiry(
            {"actions": [{"id": "jars", "type": "hunt_file",
                          "params": {"name": "*.jar"}}]}, t),
            t + timedelta(seconds=DEFAULT_STAYS_OPEN))


class CheckinSkipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = _host(1)

    def _pending_hunt_task(self, **kw):
        return Task.objects.create(
            host=self.host,
            action="_script",
            params=HUNT_PARAMS,
            nonce=("n" * 31) + str(Task.objects.count() % 10),
            ttl_seconds=300,
            state=Task.State.PENDING,
            **kw,
        )

    def test_checkin_skips_expired_pending(self):
        task = self._pending_hunt_task(expires_at=now() - timedelta(minutes=5))
        resp = self.client.post(
            "/api/v1/checkin", {"hostname": self.host.hostname},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json().get("tasks", []), [],
                         "a past-stays_open hunt task was dispatched anyway")
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.PENDING)

    def test_checkin_still_dispatches_unexpired_pending(self):
        task = self._pending_hunt_task(expires_at=now() + timedelta(days=1))
        resp = self.client.post(
            "/api/v1/checkin", {"hostname": self.host.hostname},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.DISPATCHED)
        self.assertEqual(len(resp.json()["tasks"]), 1)


class SweepDidNotReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_login(self.user)
        self.host = _host(1)

    def _task(self, **kw):
        return Task.objects.create(
            host=self.host,
            action="_script",
            params=HUNT_PARAMS,
            nonce=("n" * 31) + str(Task.objects.count() % 10),
            ttl_seconds=300,
            state=Task.State.PENDING,
            **kw,
        )

    def test_sweep_marks_did_not_report(self):
        from apps.tasks.tasks import expire_stale_tasks

        task = self._task(expires_at=now() - timedelta(hours=1))
        expire_stale_tasks()
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.EXPIRED)
        self.assertIsNotNone(task.completed_at)
        self.assertIsNone(task.dispatched_at)
        self.assertIn("[did not report:", task.result_output)
        self.assertIn("the hunt stayed open until", task.result_output)

        # A pending task without expires_at (a non-hunt) is untouched.
        other = self._task()
        expire_stale_tasks()
        other.refresh_from_db()
        self.assertEqual(other.state, Task.State.PENDING)

    def test_sweep_finalizes_run(self):
        from apps.tasks.tasks import expire_stale_tasks

        run = TaskRun.objects.create(
            name_snapshot="r", host_count=1, step_count=1,
            state=TaskRun.State.RUNNING,
        )
        self._task(expires_at=now() - timedelta(hours=1), run=run, step_order=0)
        expire_stale_tasks()
        run.refresh_from_db()
        # No host answered at all: the run failed.
        self.assertEqual(run.state, TaskRun.State.FAILED)

    def test_some_hosts_answered_run_is_partial(self):
        # User decision 2026-09-26: a hunt where some hosts finished and some
        # never reported ends partial, not failed.
        from apps.tasks.tasks import expire_stale_tasks

        run = TaskRun.objects.create(
            name_snapshot="r", host_count=2, step_count=1,
            state=TaskRun.State.RUNNING,
        )
        answered = self._task(run=run, step_order=0)
        Task.objects.filter(pk=answered.pk).update(state=Task.State.COMPLETED)
        self.host = _host(2)
        self._task(expires_at=now() - timedelta(hours=1), run=run, step_order=0)
        expire_stale_tasks()
        run.refresh_from_db()
        self.assertEqual(run.state, TaskRun.State.PARTIAL)


class ResultsApiDidNotReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_login(self.user)
        self.host = _host(1)
        self.definition = TaskDefinition.objects.create(
            owner=self.user, name="Hunt jars", yaml_source=HUNT_YAML,
            parsed_spec=parse_and_validate(HUNT_YAML),
        )

    def test_results_api_shows_did_not_report(self):
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars", host_count=2, step_count=1,
            state=TaskRun.State.RUNNING, definition=self.definition,
            requested_by=self.user,
        )
        offline = _host(2)
        Task.objects.create(
            host=offline, action="_script", params=HUNT_PARAMS,
            nonce=("n" * 31) + "0", ttl_seconds=300,
            state=Task.State.EXPIRED, run=run, step_order=0,
            completed_at=now(), expires_at=now() - timedelta(hours=1),
        )
        Task.objects.create(
            host=self.host, action="_script", params=HUNT_PARAMS,
            nonce=("n" * 31) + "1", ttl_seconds=300,
            state=Task.State.PENDING, run=run, step_order=1,
            expires_at=now() + timedelta(days=1),
        )

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 200, resp.content)
        by_host = {h["host_id"]: h for h in resp.json()["hosts"]}
        self.assertEqual(by_host[str(offline.id)]["state"], "did_not_report")
        self.assertEqual(by_host[str(self.host.id)]["state"], "pending")
