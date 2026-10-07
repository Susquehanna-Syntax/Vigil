"""Sign-in sessions end on idleness and on a hard cap (QA-14a).

The tests move time with a patched ``time.time`` rather than waiting for it.
The distinction the suite defends is the one the feature turns on: a
background ``GET /api/...`` poll is *checked* against the limits but must not
refresh the idle clock, or an open Monitor tab would never time out.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.instance import config

from . import session_timeout

T0 = 1_700_000_000.0
MIN = 60
API = "/api/v1/accounts/session/"
HEARTBEAT = "/api/v1/accounts/session/activity/"


class _ClockMixin:
    def login(self):
        """Sign in with the clock frozen at T0, and leave it frozen there.

        ``at()`` moves time only forward, so a test that never calls it still
        runs at T0 rather than at the wall clock.
        """
        user = get_user_model().objects.create_user("alice", password="pw")
        self._patcher = mock.patch(
            "apps.accounts.session_timeout.time.time", return_value=T0
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.client.force_login(user)
        return user

    def at(self, seconds):
        """Freeze the middleware's clock at T0 + *seconds*."""
        self._patcher = mock.patch(
            "apps.accounts.session_timeout.time.time", return_value=T0 + seconds
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)


class LoginStampTests(_ClockMixin, TestCase):
    def test_login_stamps_both_clocks(self):
        self.login()
        session = self.client.session
        self.assertEqual(session[session_timeout.LOGIN_AT], T0)
        self.assertEqual(session[session_timeout.LAST_ACTIVE], T0)


class IdleTimeoutTests(_ClockMixin, TestCase):
    def test_background_poll_does_not_extend(self):
        self.login()
        self.at(10 * MIN)
        self.assertEqual(self.client.get(API).status_code, 200)
        self.at(16 * MIN)
        resp = self.client.get(API)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["reason"], "session_idle")

    def test_heartbeat_extends(self):
        self.login()
        self.at(10 * MIN)
        resp = self.client.post(HEARTBEAT)
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", None))
        self.assertEqual(resp.json()["idle_seconds_left"], 900)
        self.at(20 * MIN)
        self.assertEqual(self.client.get(API).status_code, 200)

    def test_page_load_extends(self):
        self.login()
        self.at(10 * MIN)
        resp = self.client.get("/")
        self.assertIn(resp.status_code, (200, 302))
        self.assertNotIn("/login/", resp.get("Location", ""))
        self.at(20 * MIN)
        self.assertEqual(self.client.get(API).status_code, 200)

    def test_idle_page_redirects_to_login(self):
        self.login()
        self.at(16 * MIN)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"],
            "/login/?expired=idle&next=%2F",
            "the browser side keys off these query params (QA-14b)",
        )
        self.at(16 * MIN + 1)
        self.assertIn(self.client.get(API).status_code, (401, 403))


class HardCapTests(_ClockMixin, TestCase):
    def test_hard_cap(self):
        self.login()
        for minutes in range(10, 12 * 60, 10):
            self.at(minutes * MIN)
            self.assertEqual(self.client.post(HEARTBEAT).status_code, 200)
        self.at(12 * 60 * MIN + MIN)
        resp = self.client.post(HEARTBEAT)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["reason"], "session_max")


class SettingsChangeTests(_ClockMixin, TestCase):
    def test_settings_change_applies(self):
        config.write("VIGIL_SESSION_IDLE_MINUTES", "30")
        config.invalidate()
        self.login()
        self.at(20 * MIN)
        self.assertEqual(self.client.get(API).status_code, 200)


class ValidateRangeTests(TestCase):
    def test_validate_ranges(self):
        self.assertFalse(config.validate("VIGIL_SESSION_IDLE_MINUTES", "4")[0])
        self.assertTrue(config.validate("VIGIL_SESSION_IDLE_MINUTES", "5")[0])
        self.assertFalse(config.validate("VIGIL_SESSION_IDLE_MINUTES", "721")[0])
        self.assertFalse(config.validate("VIGIL_SESSION_MAX_HOURS", "0")[0])
        self.assertTrue(config.validate("VIGIL_SESSION_MAX_HOURS", "168")[0])


class AgentsUnaffectedTests(TestCase):
    def test_agents_unaffected(self):
        """Agents authenticate by bearer token and carry no session, so the
        middleware must leave them alone however long a host has been quiet."""
        host = Host.objects.create(
            hostname="legacy-box",
            agent_token="REPLACE_WITH_TOKEN",
            status=Host.Status.ONLINE,
        )
        with mock.patch(
            "apps.accounts.session_timeout.time.time", return_value=T0 + 30 * 24 * 3600
        ):
            resp = self.client.post(
                "/api/v1/checkin",
                {},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {host.agent_token}",
            )
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", None))
        self.assertNotIn("session_", resp.content.decode("utf-8", "ignore"))


class LoginPageTests(TestCase):
    def test_login_page_says_why(self):
        # Unauthenticated: an authenticated client is bounced off /login/ to
        # the dashboard by login_view itself.
        get_user_model().objects.create_user("alice", password="pw")
        body = self.client.get("/login/?expired=idle").content.decode()
        self.assertIn("period of inactivity", body)
