"""M11 07: live container logs — opened with TOTP, fed by the agent, closed by leaving."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.tasks.models import Task

from .models import Host, LogTailSession

_TOTP = "apps.accounts.totp.require_totp_confirmation"


class LogTailTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="dock", agent_token="logtok",
                                        status=Host.Status.ONLINE, mode="managed")
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def _open(self, name="web", totp_error=None):
        with mock.patch(_TOTP, return_value=totp_error):
            return self.client_api.post(f"/api/v1/hosts/{self.host.id}/containers/{name}/logs/",
                                        {"totp": "1", "tail": 50}, format="json")

    def _agent(self, session, lines):
        return self.client.post(f"/api/v1/agent/log-tail/{session}/", {"lines": lines},
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer logtok")

    def test_open_creates_a_signed_logs_task(self):
        resp = self._open()
        self.assertEqual(resp.status_code, 201, resp.content)
        task = Task.objects.get(pk=resp.json()["task"])
        step = task.params["steps"][0]
        self.assertEqual(step["action"], "container_logs")
        self.assertEqual(step["params"], {"container_name": "web", "tail": 50,
                                          "session": resp.json()["session"]})
        self.assertEqual(task.risk_level, "low")

    def test_refusals(self):
        self.assertEqual(self._open(totp_error="bad").status_code, 401)
        self.assertEqual(self._open(name="-rf").status_code, 400)
        viewer = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        c = APIClient()
        c.force_authenticate(viewer)
        self.assertEqual(c.post(f"/api/v1/hosts/{self.host.id}/containers/web/logs/", {}, format="json").status_code, 403)
        self.assertFalse(Task.objects.exists())

    def test_agent_lines_reach_the_viewer_and_stop_when_it_leaves(self):
        session = self._open().json()["session"]
        self.assertTrue(self._agent(session, ["a", "b"]).json()["continue"])
        body = self.client_api.get(f"/api/v1/hosts/log-tails/{session}/?after=0").json()
        self.assertEqual(body["lines"], [[1, "a"], [2, "b"]])
        self.assertEqual(self.client_api.get(f"/api/v1/hosts/log-tails/{session}/?after=1").json()["lines"],
                         [[2, "b"]])
        LogTailSession.objects.filter(pk=session).update(viewer_seen_at=now() - timedelta(seconds=30))
        self.assertFalse(self._agent(session, ["c"]).json()["continue"], "the viewer went away")
        self.client_api.get(f"/api/v1/hosts/log-tails/{session}/")
        self.client_api.delete(f"/api/v1/hosts/log-tails/{session}/")
        self.assertFalse(self._agent(session, []).json()["continue"], "closed")

    def test_another_hosts_agent_cannot_feed_it(self):
        session = self._open().json()["session"]
        Host.objects.create(hostname="other", agent_token="othertok", status=Host.Status.ONLINE)
        resp = self.client.post(f"/api/v1/agent/log-tail/{session}/", {"lines": ["x"]},
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer othertok")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(LogTailSession.objects.get().lines, [])

    def test_buffer_is_capped(self):
        session = self._open().json()["session"]
        for _ in range(5):
            self._agent(session, [f"l{n}" for n in range(500)])
        stored = LogTailSession.objects.get().lines
        self.assertEqual(len(stored), LogTailSession.MAX_LINES)
        self.assertEqual(stored[-1][0], 2500)


class LogViewWiringTests(TestCase):
    def test_monitor_page_wires_the_log_view(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[2] / "static/js/vigil-monitor.js").read_text(encoding="utf-8")
        for needle in ("data-ctr-logs", "async function openContainerLogs", "/logs/`",
                       "/api/v1/hosts/log-tails/", "method: 'DELETE'", "pollingInterval(_pollContainerLogs, 1500)"):
            self.assertIn(needle, js)
