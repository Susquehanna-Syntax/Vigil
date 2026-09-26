"""The not-applicable state: task and run.

A host that evaluated a task's `relevant:` block and found it does not
apply reports `not_applicable` — terminal, neither pass nor failure. The
host's chain stops without retry, a run where every host was not
applicable ends `not_applicable`, and mixed runs finish as if the
not-applicable hosts were not there.
"""

import json
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate

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


def _task(host, state=Task.State.DISPATCHED, **kw):
    t = Task.objects.create(
        host=host,
        action="_script",
        params=HUNT_PARAMS,
        nonce=("n" * 31) + str(Task.objects.count() % 10),
        ttl_seconds=300,
        state=state,
        **kw,
    )
    Task.objects.filter(pk=t.pk).update(dispatched_at=now() - timedelta(seconds=1))
    return t


def _host(n):
    return Host.objects.create(
        hostname=f"h{n}",
        agent_token=("t" * 31) + str(n),
        status=Host.Status.ONLINE,
        mode=Host.Mode.MANAGED,
    )


class NotApplicableStateTests(TestCase):
    def setUp(self):
        self.host = _host(1)
        self.client = APIClient()

    def _post(self, task, state="not_applicable", **extra):
        payload = {
            "task_id": str(task.id),
            "state": state,
            "output": "not applicable here",
        }
        payload.update(extra)
        return self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )

    def test_agent_can_report_not_applicable(self):
        task = _task(self.host)
        resp = self._post(task)
        self.assertEqual(resp.status_code, 200, resp.content)
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.NOT_APPLICABLE)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.result_output, "not applicable here")

    def test_chain_stops_without_retry(self):
        run = TaskRun.objects.create(
            name_snapshot="r", host_count=1, step_count=3,
            state=TaskRun.State.RUNNING,
        )
        step1 = _task(self.host, run=run, step_order=0, max_retries=2)
        step2 = _task(
            self.host, state=Task.State.BLOCKED,
            run=run, step_order=1, max_retries=2,
        )
        step3 = _task(
            self.host, state=Task.State.BLOCKED,
            run=run, step_order=2, max_retries=2,
        )

        resp = self._post(step1)
        self.assertEqual(resp.status_code, 200, resp.content)

        for n, step in ((2, step2), (3, step3)):
            step.refresh_from_db()
            self.assertEqual(
                step.state, Task.State.NOT_APPLICABLE,
                f"step {n} should be not_applicable, got {step.state}",
            )
            self.assertIsNotNone(
                step.completed_at, f"step {n} should have completed_at")
            self.assertEqual(
                step.retry_count, 0, f"step {n} must not count as a retry")


class NotApplicableRunOutcomeTests(TestCase):
    def setUp(self):
        self.hosts = [_host(i) for i in (1, 2)]

    def _run_with_states(self, states):
        run = TaskRun.objects.create(
            name_snapshot="r", host_count=len(states), step_count=1,
            state=TaskRun.State.RUNNING,
        )
        for i, state in enumerate(states):
            Task.objects.create(
                host=self.hosts[i % len(self.hosts)],
                action="_script",
                params=HUNT_PARAMS,
                nonce=("n" * 31) + str(Task.objects.count()),
                ttl_seconds=300,
                state=state,
                run=run,
                step_order=0,
            )
        return run

    def test_all_hosts_not_applicable_run_state(self):
        run = self._run_with_states([
            Task.State.NOT_APPLICABLE, Task.State.NOT_APPLICABLE,
        ])
        from apps.tasks.views import _finalize_run_if_done

        _finalize_run_if_done(run)
        self.assertEqual(run.state, TaskRun.State.NOT_APPLICABLE)

    def test_mixed_run_ignores_not_applicable(self):
        from apps.tasks.views import _finalize_run_if_done

        cases = (
            ([Task.State.COMPLETED, Task.State.NOT_APPLICABLE],
             TaskRun.State.COMPLETED),
            ([Task.State.FAILED, Task.State.NOT_APPLICABLE],
             TaskRun.State.FAILED),
            ([Task.State.COMPLETED, Task.State.FAILED, Task.State.NOT_APPLICABLE],
             TaskRun.State.PARTIAL),
        )
        for states, expected in cases:
            with self.subTest(states=states):
                run = self._run_with_states(states)
                _finalize_run_if_done(run)
                self.assertEqual(run.state, expected)


class NotApplicableHuntResultsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client = APIClient()
        self.client.force_login(self.user)
        self.host = _host(1)
        self.definition = TaskDefinition.objects.create(
            owner=self.user, name="Hunt jars", yaml_source=HUNT_YAML,
            parsed_spec=parse_and_validate(HUNT_YAML),
        )

    def test_hunt_results_show_not_applicable(self):
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars", host_count=1, step_count=1,
            state=TaskRun.State.RUNNING, definition=self.definition,
            requested_by=self.user,
        )
        Task.objects.create(
            host=self.host, action="_script", params=HUNT_PARAMS,
            nonce="n" * 31 + "0", ttl_seconds=300,
            state=Task.State.NOT_APPLICABLE, run=run, step_order=0,
            completed_at=now(),
        )

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 200, resp.content)
        hosts = resp.json()["hosts"]
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["state"], "not_applicable")


class NotApplicableUiMapsTests(TestCase):
    def test_ui_maps_know_not_applicable(self):
        static_js = Path(__file__).resolve().parents[2] / "static/js"
        runhistory = (static_js / "vigil-runhistory.js").read_text()
        self.assertIn("not_applicable: 'lav'", runhistory)

        tasks_js = (static_js / "vigil-tasks.js").read_text()
        self.assertIn("not_applicable: 'var(--lavender)'", tasks_js)
        self.assertIn("not_applicable: 'Not applicable'", tasks_js)

        hunts_js = (static_js / "vigil-hunts.js").read_text()
        self.assertIn("not_applicable: 'lav'", hunts_js)
        self.assertIn("not_applicable: 'Not applicable'", hunts_js)

        template = (
            Path(__file__).resolve().parents[2]
            / "templates/pages/_hunt_results.html"
        ).read_text()
        self.assertIn('value="not_applicable"', template)
