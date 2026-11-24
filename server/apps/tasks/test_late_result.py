"""A result the agent held while the server could not take it (rc2).

The dev PC's update failed with a relay 503 and its failure report hit the
same 503; the agent now resends it after its next check-in. By then the expiry
sweep may have marked the task EXPIRED, which was only the server's guess."""

import json

from django.test import TestCase
from django.utils import timezone

from apps.hosts.models import Host
from apps.tasks.models import Task


class LateResultTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)

    def _task(self, note, nonce):
        return Task.objects.create(host=self.host, action="update_agent", params={},
                                   nonce=nonce, state=Task.State.EXPIRED,
                                   completed_at=timezone.now(), result_output=note)

    def _post(self, task, state="failed", output="503 Server Error on download"):
        return self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps({"task_id": str(task.id), "state": state, "output": output}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_the_real_outcome_replaces_the_sweeps_guess(self):
        task = self._task("[expired by server: no result 3900s after dispatch]", "a" * 32)
        resp = self._post(task)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertIn("503 Server Error on download", task.result_output)
        self.assertIn("arrived late", task.result_output)

    def test_an_update_an_operator_replaced_takes_its_late_result_too(self):
        task = self._task("[replaced by a new update request from op]", "b" * 32)
        self.assertEqual(self._post(task, "completed", "updated").status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.COMPLETED)

    def test_a_task_that_finished_normally_still_refuses_a_second_result(self):
        task = self._task("done", "c" * 32)
        Task.objects.filter(pk=task.pk).update(state=Task.State.COMPLETED)
        self.assertEqual(self._post(task).status_code, 400)
