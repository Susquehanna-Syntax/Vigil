"""Starting a session: who gets one, who is refused, and what the refusal costs.

The order of the checks in ``views._start`` is the security contract, so it is
asserted here one refusal at a time — each with its status *and* its audit row,
because "who tried to shell into a machine and why did Vigil say no" has to be
answerable afterwards from the log alone.
"""

import asyncio
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps_business.sites.models import (
    HostSiteAssignment,
    Site,
    SiteCapability,
    UserSiteRole,
)

from ..bus import WAKE_KEY, drain, enqueue, peek
from ..models import RemoteSession
from ..tickets import make_ticket, verify_ticket

#: Not a real host: the wake queue is keyed by id and nothing here dials it.
AGENT_TOKEN = "a" * 64


def make_user(username: str, role=str) -> get_user_model():
    user = get_user_model().objects.create_user(username=username, password="pw")
    UserProfile.objects.create(user=user, role=role)
    return user


def enroll_totp(user, secret: str = "JBSWY3DPEHPK3PXP") -> str:
    """Enrol *user* for real, and hand back a code that verifies right now."""
    from django.utils.timezone import now

    from apps.accounts.totp import generate_totp

    profile = user.profile
    profile.totp_secret = secret
    profile.totp_confirmed_at = now()
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at"])
    return generate_totp(secret)


class _Audited(TestCase):
    """``refusals()``/``started()`` without the audit app's name in every test."""

    def refusals(self):
        return self._events().filter(action="remote.session_refused")

    def started(self):
        return self._events().filter(action="remote.session_started")

    def _events(self):
        from apps_business.audits.models import AuditEvent

        return AuditEvent.objects.all()


class StartApiTests(_Audited):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="shell-host", agent_token=AGENT_TOKEN,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
            agent_allow_remote_shell=True)
        self.admin = make_user("root", Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def post(self, **body):
        payload = {"host": str(self.host.id), "kind": "shell"}
        payload.update(body)
        return self.client.post("/api/v1/sessions/", payload, format="json")

    # -- the happy path ----------------------------------------------------

    def test_admin_with_totp_on_an_opted_in_host_gets_a_ticket_the_agent_accepts(self):
        code = enroll_totp(self.admin)

        resp = self.post(totp=code)

        self.assertEqual(resp.status_code, 201, resp.content)
        session = RemoteSession.objects.get(pk=resp.json()["id"])
        self.assertEqual(resp.json()["ws"], f"/ws/session/{session.id}/")
        self.assertEqual(session.state, RemoteSession.State.PENDING)
        self.assertEqual(session.username, "root")
        self.assertEqual([e.action for e in self.started()], ["remote.session_started"])

        queue = peek(WAKE_KEY.format(host_id=self.host.id))
        self.assertEqual(len(queue), 1)
        # The whole point of the ticket: verified with the agent's own token it
        # names this session and nothing else. A relay that intercepted the wake
        # response could neither read a different host's session out of it nor
        # rewrite it to point at one of its own.
        body = verify_ticket(queue[0], agent_token=AGENT_TOKEN)
        self.assertEqual(body["sid"], str(session.id))

    def test_a_container_session_keeps_the_name_that_will_be_shelled_into(self):
        code = enroll_totp(self.admin)
        resp = self.post(kind="container", container="web-1", totp=code)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(RemoteSession.objects.get().container, "web-1")

    # -- refusals ----------------------------------------------------------

    def test_viewer_is_refused_and_the_refusal_is_written_down(self):
        self.client.force_authenticate(make_user("watcher", Role.VIEWER))
        resp = self.post()
        self.assertEqual(resp.status_code, 403)
        self._assert_refused("no hosts:shell")

    def test_operator_without_the_shell_grant_is_refused(self):
        # A per-site grant of hosts:edit must not buy a root shell on the box.
        operator = make_user("op", Role.OPERATOR)
        row = UserSiteRole.objects.create(user=operator, site=_global_site(),
                                          role=Role.OPERATOR)
        SiteCapability.objects.create(user_site_role=row, app="hosts", verb="edit")
        self.client.force_authenticate(operator)

        resp = self.post()
        self.assertEqual(resp.status_code, 403)
        self._assert_refused("no hosts:shell")

    def test_a_host_that_has_not_opted_in_locally_says_how_to_turn_it_on(self):
        Host.objects.filter(pk=self.host.pk).update(agent_allow_remote_shell=False)
        code = enroll_totp(self.admin)

        resp = self.post(totp=code)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["opt_in"], "allow_remote_shell")
        self.assertIn("does not allow remote sessions", resp.json()["error"])
        self._assert_refused("allow_remote_shell")

    def test_an_rdp_session_asks_for_the_desktop_opt_in_instead(self):
        Host.objects.filter(pk=self.host.pk).update(agent_allow_remote_desktop=False)
        code = enroll_totp(self.admin)

        resp = self.post(kind="rdp", totp=code)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["opt_in"], "allow_remote_desktop")

    def test_monitor_mode_hosts_are_out_of_reach_even_for_an_admin(self):
        Host.objects.filter(pk=self.host.pk).update(mode=Host.Mode.MONITOR)
        code = enroll_totp(self.admin)

        resp = self.post(totp=code)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "This host is in monitor mode.")
        self._assert_refused("monitor mode")

    def test_a_user_with_no_second_factor_is_told_to_enrol_one(self):
        resp = self.post(totp="123456")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json()["error"],
            "Set up two-factor authentication before opening a session.")
        self._assert_refused("no TOTP enrolled")

    def test_a_wrong_code_is_refused_as_the_credential_it_failed_to_be(self):
        enroll_totp(self.admin)
        with patch("apps.accounts.totp.consume_totp",
                   return_value=(False, "Invalid TOTP code")):
            resp = self.post(totp="000000")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "Invalid TOTP code")
        self._assert_refused("Invalid TOTP code")

    def test_a_container_name_that_is_shell_syntax_never_reaches_the_agent(self):
        code = enroll_totp(self.admin)
        for bad in ("a;rm -rf /", "-privileged", "", "../etc", "web 1", "a" * 129,
                    "web-1\n"):
            with self.subTest(container=bad):
                resp = self.post(kind="container", container=bad, totp=code)
                self.assertEqual(resp.status_code, 400, f"{bad!r} was accepted")
        self.assertEqual(RemoteSession.objects.count(), 0)

    def test_a_fourth_open_session_waits_for_one_of_the_first_three(self):
        enroll_totp(self.admin)
        # The code is spent on each start in reality; the check that matters
        # here is what happens once three sessions are already open.
        with patch("apps.accounts.totp.consume_totp", return_value=(True, None)):
            for _ in range(3):
                self.assertEqual(self.post(totp="111111").status_code, 201)
            resp = self.post(totp="111111")

        self.assertEqual(resp.status_code, 429)
        self._assert_refused("already open", sessions=3)

    # -- the vocabulary itself --------------------------------------------

    def test_legacy_operator_cannot_shell_because_it_was_never_meant_to(self):
        # LEGACY_OPERATOR is what an operator could do before per-site grants
        # existed. A root shell was not one of those things, and adding it here
        # would hand one to every existing operator the moment they upgraded.
        from apps.accounts.permissions import LEGACY_OPERATOR

        self.assertNotIn(("hosts", "shell"), set(LEGACY_OPERATOR))
        self.assertIn(("hosts", "view"), set(LEGACY_OPERATOR))

    def test_a_host_that_cannot_be_seen_is_not_found_rather_than_denied(self):
        # So a scoped user cannot probe for hosts in sites they are not in.
        elsewhere = Site.objects.create(name="Elsewhere", slug="elsewhere")
        HostSiteAssignment.objects.create(host=self.host, site=elsewhere)
        mine = Site.objects.create(name="Mine", slug="mine")
        scoped = make_user("limited", Role.VIEWER)
        UserSiteRole.objects.create(user=scoped, site=mine, role=Role.ADMIN)

        self.client.force_authenticate(scoped)
        resp = self.post()
        self.assertEqual(resp.status_code, 404)
        self._assert_refused("unknown host")

    def _assert_refused(self, reason_part: str, sessions: int = 0):
        rows = self.refusals()
        self.assertEqual(rows.count(), 1, f"one refusal row expected for {reason_part!r}")
        self.assertIn(reason_part, rows.get().detail["reason"])
        self.assertEqual(RemoteSession.objects.count(), sessions)


def _global_site():
    return Site.objects.global_site()


class CheckinOptInTests(TestCase):
    """The two opt-in fields are written only by the agent's own check-in."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="optin", agent_token=AGENT_TOKEN, status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED)

    def checkin(self, **payload):
        return self.client.post(
            "/api/v1/checkin", payload, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_the_agent_turns_remote_shell_on_and_off_again(self):
        resp = self.checkin(hostname="optin", allow_remote_shell=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.host.refresh_from_db()
        self.assertTrue(self.host.agent_allow_remote_shell)

        self.checkin(hostname="optin", allow_remote_shell=False)
        self.host.refresh_from_db()
        self.assertFalse(self.host.agent_allow_remote_shell)

    def test_a_checkin_saying_nothing_leaves_the_choice_alone(self):
        Host.objects.filter(pk=self.host.pk).update(agent_allow_remote_shell=True)
        self.checkin(hostname="optin")
        self.host.refresh_from_db()
        self.assertTrue(self.host.agent_allow_remote_shell,
                        "a check-in that omits the field must not silently revoke it")

    def test_the_desktop_opt_in_is_a_separate_switch(self):
        self.checkin(hostname="optin", allow_remote_desktop=True)
        self.host.refresh_from_db()
        self.assertTrue(self.host.agent_allow_remote_desktop)
        self.assertFalse(self.host.agent_allow_remote_shell)


class SessionListTests(_Audited):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="listed", agent_token=AGENT_TOKEN, mode=Host.Mode.MANAGED)
        self.admin = make_user("root", Role.ADMIN)
        self.session = RemoteSession.objects.create(
            host=self.host, user=self.admin, username="root", kind="shell",
            nonce="n" * 32, recording="recordings/x.cast")

    def test_listing_a_host_shows_its_sessions_with_their_counters(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/v1/sessions/?host={self.host.id}")
        self.assertEqual(resp.status_code, 200, resp.content)
        row = resp.json()["sessions"][0]
        self.assertEqual(row["id"], str(self.session.id))
        self.assertEqual(row["username"], "root")
        for field in ("state", "created_at", "close_reason", "bytes_in", "bytes_out"):
            self.assertIn(field, row)

    def test_only_an_admin_on_a_business_license_may_be_told_a_recording_exists(self):
        with patch("apps.remote.views.has_feature", return_value=True):
            self.client.force_authenticate(self.admin)
            mine = self.client.get(f"/api/v1/sessions/?host={self.host.id}").json()
            self.assertTrue(mine["sessions"][0]["has_recording"])

            viewer = make_user("watcher2", Role.VIEWER)
            self.client.force_authenticate(viewer)
            theirs = self.client.get(f"/api/v1/sessions/?host={self.host.id}").json()
        self.assertFalse(theirs["sessions"][0]["has_recording"])

    def test_anonymous_may_not_list_anything(self):
        # DRF answers an anonymous session-auth request with 403.
        self.assertIn(self.client.get("/api/v1/sessions/").status_code, (401, 403))


class RecordingDownloadTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = make_user("root", Role.ADMIN)
        self.host = Host.objects.create(hostname="taped", agent_token=AGENT_TOKEN)
        self.session = RemoteSession.objects.create(
            host=self.host, user=self.admin, username="root", kind="shell", nonce="n")

    def test_without_the_feature_there_is_no_recording_to_be_had(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/v1/sessions/{self.session.id}/recording/")
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["feature"], "audit_log")

    def test_a_session_with_no_file_is_404_rather_than_an_empty_download(self):
        self.client.force_authenticate(self.admin)
        with patch("apps.remote.views.has_feature", return_value=True):
            resp = self.client.get(f"/api/v1/sessions/{self.session.id}/recording/")
        self.assertEqual(resp.status_code, 404)

    def test_a_viewer_cannot_download_evidence_about_someone_elses_session(self):
        self.client.force_authenticate(make_user("nosy", Role.VIEWER))
        resp = self.client.get(f"/api/v1/sessions/{self.session.id}/recording/")
        self.assertEqual(resp.status_code, 403)


class CloseEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = make_user("owner", Role.ADMIN)
        self.host = Host.objects.create(hostname="closer", agent_token=AGENT_TOKEN)
        self.session = RemoteSession.objects.create(
            host=self.host, user=self.owner, username="owner", kind="shell", nonce="n")

    def _close(self):
        return self.client.post(f"/api/v1/sessions/{self.session.id}/close/")

    def test_closing_reaches_both_ends_of_the_pipe(self):
        self.client.force_authenticate(self.owner)
        resp = self._close()
        self.assertEqual(resp.status_code, 202, resp.content)

        for direction in ("b2a", "a2b"):
            queued = peek(f"vigil:sess:{self.session.id}:{direction}")
            self.assertEqual([item["t"] for item in queued], ["close"], direction)
            self.assertIn("closed by owner", queued[0]["reason"])

    def test_a_stranger_cannot_interrupt_someone_elses_session(self):
        self.client.force_authenticate(make_user("rando", Role.VIEWER))
        self.assertEqual(self._close().status_code, 403)


class WakeViewTests(TestCase):
    """The agent's long-poll. The timeout is patched so the suite never sleeps."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(hostname="waker", agent_token=AGENT_TOKEN,
                                        status=Host.Status.ONLINE)
        self.session = RemoteSession.objects.create(
            host=self.host, user=None, username="root", kind="shell", nonce="n")
        self.ticket = make_ticket(self.session)

    def key(self):
        return WAKE_KEY.format(host_id=self.host.id)

    def get(self, token=AGENT_TOKEN):
        return self.client.get("/api/v1/agent/wake",
                               HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_ticket_waiting_is_handed_over_at_once(self):
        enqueue(self.key(), self.ticket)
        with patch("apps.remote.views.WAKE_SECONDS", 0.2):
            resp = self.get()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([t["sid"] for t in resp.json()["sessions"]],
                         [str(self.session.id)])

    def test_nothing_to_report_is_an_empty_list_not_an_error(self):
        with patch("apps.remote.views.WAKE_SECONDS", 0.1):
            resp = self.get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"sessions": []})

    def test_a_ticket_that_arrives_during_the_wait_is_returned(self):
        import threading

        later = threading.Timer(0.2, enqueue, args=(self.key(), self.ticket))
        later.start()
        try:
            with patch("apps.remote.views.WAKE_SECONDS", 5.0):
                resp = self.get()
        finally:
            later.cancel()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["sid"] for t in resp.json()["sessions"]],
                         [str(self.session.id)])

    def test_an_expired_ticket_is_dropped_not_forwarded(self):
        enqueue(self.key(), dict(self.ticket, exp=1))
        with patch("apps.remote.views.WAKE_SECONDS", 0.1):
            resp = self.get()
        self.assertEqual(resp.json(), {"sessions": []})

    def test_an_unknown_token_is_refused_as_any_bad_agent_credential_is(self):
        with patch("apps.remote.views.WAKE_SECONDS", 0.1):
            resp = self.get(token="nope-nope-nope")
        self.assertEqual(resp.status_code, 401)

    def test_a_direct_wake_is_refused_once_the_builtin_relay_is_off(self):
        # The same gate every other agent path passes. Without
        # /api/v1/agent/wake in AGENT_PATHS this call would slip past it, and a
        # caller outside the relay would learn which hosts have sessions waiting.
        from apps.relays.models import Relay

        Relay.objects.filter(kind=Relay.Kind.BUILTIN).update(enabled=False)
        with patch("apps.remote.views.WAKE_SECONDS", 0.1):
            resp = self.get()
        self.assertEqual(resp.status_code, 403)
        self.assertIn("relay", resp.json()["error"].lower())

    def test_a_post_is_not_how_an_agent_asks_for_work(self):
        resp = self.client.post("/api/v1/agent/wake", {}, format="json")
        self.assertEqual(resp.status_code, 405)
