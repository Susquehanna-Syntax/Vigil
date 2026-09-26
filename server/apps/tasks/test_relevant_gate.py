"""relevant: on the server side of phase 04 — the feature gate and probe evidence.

An agent that cannot read a ``relevant:`` block would ignore it and run the
fix on every host it was sent to, so check-in refuses such a task for any
host that has not said (in its check-in ``features``) that it understands
the block. The probes the agent does run are stored as hunt evidence.
"""
import json
import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.tasks.models import HuntMatch, Task

_CHECKIN = "/api/v1/checkin"

RELEVANT = {"op": "all", "items": [
    {"probe": {"id": "relevant-1", "type": "hunt_package",
               "params": {"name": "openssl", "version_lt": "3.0.13"}}, "risk": 0},
    {"op": "not", "items": [
        {"probe": {"id": "relevant-2", "type": "hunt_file",
                   "params": {"name": "skip-openssl"}}, "risk": 0},
    ]},
]}
STEPS = [{"id": "upd", "action": "check_service", "params": {"service_name": "cron"}}]


def _task(host, params, state=Task.State.PENDING):
    return Task.objects.create(
        host=host, action="_script", params=params, state=state,
        nonce=secrets.token_hex(32),
    )


class FeatureGateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-01", agent_token="tok-" + "r" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def _checkin(self, **extra):
        return self.client.post(
            _CHECKIN, {"hostname": self.host.hostname, **extra}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )

    def test_old_agent_is_refused(self):
        task = _task(self.host, {"steps": STEPS, "relevant": RELEVANT})
        resp = self._checkin()  # an old agent sends no features at all
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tasks"], [])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertIn("does not understand relevant:", task.result_output)
        self.assertIsNotNone(task.completed_at)

    def test_new_agent_gets_it(self):
        # Features arrive in the same check-in that picks the task up.
        task = _task(self.host, {"steps": STEPS, "relevant": RELEVANT})
        resp = self._checkin(features=["relevant"])
        self.assertEqual([t["id"] for t in resp.json()["tasks"]], [str(task.id)])
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_features, ["relevant"])

    def test_tasks_without_relevant_unaffected(self):
        task = _task(self.host, {"steps": STEPS})
        resp = self._checkin()
        self.assertEqual([t["id"] for t in resp.json()["tasks"]], [str(task.id)])

    def test_garbage_features_grant_nothing(self):
        task = _task(self.host, {"steps": STEPS, "relevant": RELEVANT})
        self._checkin(features=["relevant", {"x": 1}])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_features, [])


class ProbeEvidenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-02", agent_token="tok-" + "s" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def test_probe_evidence_is_stored(self):
        task = _task(self.host, {"steps": STEPS, "relevant": RELEVANT},
                     state=Task.State.DISPATCHED)
        probe = {"id": "relevant-1", "status": "ok",
                 "result": {"matched": True, "count": 1, "truncated": False},
                 # A spoofed action in the report must not win.
                 "action": "hunt_file",
                 "hunt": {"matches": [{"evidence_type": "package", "name": "openssl",
                                       "version": "3.0.2"}],
                          "truncated": False, "duration": 0.1, "timed_out": False}}
        resp = self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps({"task_id": str(task.id), "state": "not_applicable",
                             "output": "[NOT APPLICABLE]", "steps": [probe]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        row = HuntMatch.objects.get(task=task)
        self.assertEqual((row.step_id, row.action), ("relevant-1", "hunt_package"))
        self.assertEqual(row.data["version"], "3.0.2")
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.NOT_APPLICABLE)
