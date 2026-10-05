"""Agent-reported step results reach the server, get sanitised, and store.

The agent posts `steps: [{id, status, result}]` to the result endpoint.
`_clean_step_results` trims the untrusted payload (entry count, key and
string length, allowed value types, total size) before anything touches
the Task; old agents that send no steps keep working.
"""

import json
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.hosts.models import Host
from apps.tasks.models import Task

now = timezone.now


def _task(host):
    t = Task.objects.create(
        host=host,
        action="_script",
        params={"steps": []},
        nonce=("n" * 31) + str(Task.objects.count() % 10),
        ttl_seconds=300,
        state=Task.State.DISPATCHED,
    )
    Task.objects.filter(pk=t.pk).update(dispatched_at=now() - timedelta(seconds=1))
    return t


class StepResultStorageTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="h",
            agent_token="t" * 32,
            status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED,
        )

    def _post(self, task, **extra):
        payload = {
            "task_id": str(task.id),
            "state": "completed",
            "output": "done",
        }
        payload.update(extra)
        return self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )

    def test_step_results_are_stored(self):
        task = _task(self.host)
        steps = [{"id": "svc", "status": "ok", "result": {"active": True}}]
        resp = self._post(task, steps=steps)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.result_data, {"steps": steps})

    def test_old_agent_without_steps_still_works(self):
        task = _task(self.host)
        resp = self._post(task)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.result_data, {})

    def test_non_simple_values_are_dropped(self):
        task = _task(self.host)
        steps = [
            {
                "id": "svc",
                "status": "ok",
                "result": {
                    "active": True,
                    "nested": {"x": 1},
                    "long": "y" * 5000,
                },
            }
        ]
        self._post(task, steps=steps)
        task.refresh_from_db()
        result = task.result_data["steps"][0]["result"]
        self.assertEqual(result["active"], True)
        self.assertNotIn("nested", result)
        self.assertEqual(len(result["long"]), 4096)

    def test_oversized_results_are_dropped(self):
        task = _task(self.host)
        steps = [
            {"id": f"s{i}", "status": "ok", "result": {"v": "x" * 4000}}
            for i in range(200)
        ]
        resp = self._post(task, steps=steps)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.result_data["steps"], [])
        self.assertEqual(task.result_data["dropped"], "step results exceeded 64 KB")

    def test_bad_status_entries_are_skipped(self):
        task = _task(self.host)
        steps = [{"id": "weird", "status": "weird", "result": {}}]
        self._post(task, steps=steps)
        task.refresh_from_db()
        self.assertEqual(task.result_data, {"steps": []})

    def test_run_detail_includes_result_data(self):
        from apps.tasks.serializers import TaskSerializer

        task = _task(self.host)
        task.state = Task.State.COMPLETED
        task.result_data = {"steps": []}
        task.save()
        data = TaskSerializer(task).data
        self.assertIn("result_data", data)
        self.assertEqual(data["result_data"], {"steps": []})

    def test_run_detail_js_escapes_results(self):
        from pathlib import Path

        js = Path(__file__).resolve().parents[2] / "static/js/vigil-runhistory.js"
        source = js.read_text()
        self.assertIn("_stepResultsHtml", source)
        self.assertIn("escHtml(String(v))", source)



class NonFiniteFloatTests(SimpleTestCase):
    """NaN / Infinity: the API parser refuses them, and the sanitiser drops
    them anyway — PostgreSQL's jsonb cannot store them."""

    def test_sanitiser_drops_non_finite_floats(self):
        from apps.tasks.views import _clean_step_results

        out = _clean_step_results(
            [{"id": "a", "status": "ok",
              "result": {"x": float("nan"), "y": float("inf"), "z": 1.5}}])
        self.assertEqual(out["steps"][0]["result"], {"z": 1.5})
