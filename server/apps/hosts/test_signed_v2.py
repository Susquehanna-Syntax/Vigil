"""SEC-4, server half: an agent that advertises signed_v2 gets tasks signed over
their dispatch time and the agent's own identity. The signature is checked with
the agent's real verifier, so the two ends cannot drift apart unnoticed."""
import sys
from pathlib import Path

from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import Task
from vigil.signing import get_signing_key

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "agent"))
from vigil_agent import verify as agent_verify  # noqa: E402


class SignedV2CheckinTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="tok-h",
                                        status=Host.Status.ONLINE, mode="managed")
        self.task = Task.objects.create(host=self.host, action="check_service", nonce="nonce-1",
                                        params={"service_name": "cron"}, state=Task.State.PENDING)

    def _checkin(self, features):
        return self.client.post("/api/v1/checkin", {"hostname": "h", "metrics": {}, "features": features},
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer tok-h")

    def test_v2_agent_gets_a_signature_its_verifier_accepts(self):
        body = self._checkin(["relevant", "branches", "boost", "signed_v2"]).json()
        task = body["tasks"][0]
        self.assertEqual(task["sig_v"], 2)
        key = get_signing_key().verify_key
        self.assertTrue(agent_verify.verify_task_signature(task, key, "tok-h"))
        self.assertFalse(agent_verify.verify_task_signature(task, key, "another-agent"))
        tampered = {**task, "dispatched_at": "2099-01-01T00:00:00+00:00"}
        self.assertFalse(agent_verify.verify_task_signature(tampered, key, "tok-h"))

    def test_older_agents_still_get_v1(self):
        task = self._checkin(["relevant", "branches", "boost"]).json()["tasks"][0]
        self.assertNotIn("sig_v", task)
        self.assertTrue(agent_verify.verify_task_signature(task, get_signing_key().verify_key))
