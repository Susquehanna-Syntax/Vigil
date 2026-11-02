"""An agent's terminal result announces task_finished, once, after it is saved."""

import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.hosts.models import Host
from apps.tasks.models import Task
from vigil import hooks


def _listen_until_cleanup(test, event, handler):
    """Subscribe for one test. Only this handler is removed afterwards:
    hooks.clear() would also drop the apps' own listeners for later tests."""
    hooks.subscribe(event, handler)
    test.addCleanup(lambda: hooks._subscribers[event].remove(handler))


class TaskFinishedHookTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="t" * 32,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        self.heard = []
        _listen_until_cleanup(self, "task_finished", self._listen)

    def _listen(self, task=None, host=None, **_):
        task.refresh_from_db()
        self.heard.append((task.pk, task.state, host.pk))

    def _task(self, nonce):
        task = Task.objects.create(host=self.host, action="_script", params={"steps": []},
                                   nonce=nonce, ttl_seconds=300, state=Task.State.DISPATCHED)
        Task.objects.filter(pk=task.pk).update(
            dispatched_at=timezone.now() - timedelta(seconds=1))
        return task

    def _post(self, task, state):
        return self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps({"task_id": str(task.id), "state": state, "output": "x"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_completed_and_failed_results_are_announced(self):
        done, broke = self._task("n" * 32), self._task("m" * 32)
        self.assertEqual(self._post(done, "completed").status_code, 200)
        self.assertEqual(self._post(broke, "failed").status_code, 200)
        self.assertEqual(self.heard, [(done.pk, "completed", self.host.pk),
                                      (broke.pk, "failed", self.host.pk)])

    def test_a_rejected_result_is_announced(self):
        task = self._task("j" * 32)
        self._post(task, "rejected")
        self.assertEqual(self.heard, [(task.pk, "rejected", self.host.pk)])

    def test_a_refused_update_is_not_announced(self):
        task = self._task("r" * 32)
        self.assertEqual(self._post(task, "running").status_code, 400)
        self.assertEqual(self.heard, [])

    def test_automations_are_still_not_sent_task_completed(self):
        sent = []
        _listen_until_cleanup(self, "task_completed", lambda **kw: sent.append(kw))
        self._post(self._task("c" * 32), "completed")
        self.assertEqual(sent, [])
