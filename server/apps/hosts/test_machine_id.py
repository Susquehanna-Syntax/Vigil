"""Hosts carry a stable machine_id reported by the agent.

The agent's token was the only identity a host had, so re-enrolling a machine
created a second record. The machine_id (systemd machine-id / Windows
MachineGuid / macOS IOPlatformUUID) is stored on the row so phase 03 can
replace the old record at approval time. This file pins the storage
behaviour — it changes no enrolment behaviour yet.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host


class MachineIdRegisterTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _register(self, token, hostname="web-01", **extra):
        return self.client.post(
            "/api/v1/register",
            {"agent_token": token, "hostname": hostname, **extra},
            format="json",
        )

    def test_register_stores_the_machine_id(self):
        resp = self._register("tok-" + "a" * 32, machine_id="mid-1")
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        host = Host.objects.get(agent_token="tok-" + "a" * 32)
        self.assertEqual(host.machine_id, "mid-1")

    def test_register_backfills_machine_id_for_an_existing_token(self):
        self._register("tok-" + "b" * 32)
        resp = self._register("tok-" + "b" * 32, machine_id="mid-2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Host.objects.filter(agent_token="tok-" + "b" * 32).count(), 1)
        host = Host.objects.get(agent_token="tok-" + "b" * 32)
        self.assertEqual(host.machine_id, "mid-2")

    def test_register_without_a_machine_id_still_works(self):
        resp = self._register("tok-" + "c" * 32)
        self.assertEqual(resp.status_code, 201)
        host = Host.objects.get(agent_token="tok-" + "c" * 32)
        self.assertEqual(host.machine_id, "")

    def test_a_second_token_still_creates_a_second_host(self):
        """Preservation: no dedupe happens in this phase (that is phase 03).

        Same machine, same fingerprint, different token → two rows, the new
        one pending. Phase 03 changes this.
        """
        self._register("tok-" + "d" * 32, hostname="same-box", machine_id="mid-x")
        resp = self._register("tok-" + "e" * 32, hostname="same-box", machine_id="mid-x")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Host.objects.filter(hostname="same-box").count(), 2)
        new_host = Host.objects.get(agent_token="tok-" + "e" * 32)
        self.assertEqual(new_host.status, Host.Status.PENDING)
        self.assertEqual(new_host.machine_id, "mid-x")


class MachineIdCheckinTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-01",
            agent_token="tok-" + "f" * 32,
            status=Host.Status.ONLINE,
            mode=Host.Mode.MONITOR,
        )

    def test_checkin_backfills_the_machine_id(self):
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "web-01", "machine_id": "mid-3"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.machine_id, "mid-3")


if __name__ == "__main__":
    import unittest
    unittest.main()
