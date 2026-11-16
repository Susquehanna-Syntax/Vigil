"""An agent reports how its self-update ended (sprint M8, the agent's
update_guard). The report is the agent's own claim: stored for people to see,
cleaned on the way in, and never something a host's permissions hang on."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

TOKEN = "u" * 32


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
