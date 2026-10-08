"""Only an approved host may use the agent endpoints that hand out secrets or
change state (architect review, 2026-10-08). Registering is open by design, so
before this a host nobody had approved could fetch every registry credential
scoped to "all hosts" just by registering. Check-in alone still accepts a
pending host: that is how it reaches the enrolment queue."""
from django.test import TestCase

from apps.hosts.models import Host
from apps.stacks.models import RegistryCredential
from vigil.signing import get_signing_key  # noqa: F401  (settings import order)


def _cred():
    from apps.hosts.crypto import encrypt_secret
    return RegistryCredential.objects.create(registry="ghcr.io", username="bot",
                                             password_encrypted=encrypt_secret("s3cret"), host_tags=[])


class AgentAuthApprovalTests(TestCase):
    def _get(self, token):
        return self.client.get("/api/v1/agent/registry-auth/?registry=ghcr.io",
                               HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_pending_host_gets_no_credentials(self):
        _cred()
        Host.objects.create(hostname="new", agent_token="tok-new", status=Host.Status.PENDING)
        resp = self._get("tok-new")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("s3cret", resp.content.decode())

    def test_a_rejected_host_gets_no_credentials(self):
        _cred()
        Host.objects.create(hostname="bad", agent_token="tok-bad", status=Host.Status.REJECTED)
        self.assertEqual(self._get("tok-bad").status_code, 403)

    def test_an_approved_host_still_does(self):
        _cred()
        Host.objects.create(hostname="ok", agent_token="tok-ok", status=Host.Status.ONLINE)
        resp = self._get("tok-ok")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["password"], "s3cret")

    def test_a_pending_host_can_still_check_in(self):
        Host.objects.create(hostname="new", agent_token="tok-new", status=Host.Status.PENDING)
        resp = self.client.post("/api/v1/checkin", {"hostname": "new", "metrics": {}},
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer tok-new")
        self.assertNotEqual(resp.status_code, 403)
