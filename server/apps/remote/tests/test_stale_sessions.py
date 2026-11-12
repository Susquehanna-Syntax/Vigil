"""Sessions nobody is attached to: closing one from the dashboard, and the beat
sweep that frees a slot when there is no dashboard open to close it from.

A row is only ever left PENDING/OPEN by a socket that died — a restart, a
killed worker, an agent that never answered — and each such row holds one of
the user's three session slots until something closes it.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from vigil.testsupport import require_business_module

# Exercises core with the Business apps. The sweep is a background job over
# every site and its tests must run in one — exactly how Celery runs it.
require_business_module()

from .. import tasks
from ..models import RemoteSession
from ..ws import _finish

AGENT_TOKEN = "b" * 64


def make_user(username: str) -> get_user_model():
    user = get_user_model().objects.create_user(username=username, password="pw")
    UserProfile.objects.create(user=user, role=Role.ADMIN)
    return user


def make_host(hostname: str) -> Host:
    return Host.objects.create(hostname=hostname, agent_token=AGENT_TOKEN,
                               mode=Host.Mode.MANAGED)


def make_session(user, host, state=str, moment=None, created=timedelta(0),
                 opened=timedelta(0), seen=timedelta(0)) -> RemoteSession:
    """A row in *state* created *created* before *moment*, opened *opened*
    before it and last heard from *seen* before it. Pass ``None`` for a clock
    this row does not run."""
    moment = now() if moment is None else moment

    def age(delta):
        return None if delta is None else moment - delta

    session = RemoteSession.objects.create(
        host=host, user=user, username=user.username, kind="shell",
        nonce="n" * 32, state=state)
    # created_at is auto_now_add: only .update() can move it.
    RemoteSession.objects.filter(pk=session.pk).update(
        created_at=age(created), opened_at=age(opened), last_seen=age(seen))
    session.refresh_from_db()
    return session


def active_count(user) -> int:
    return RemoteSession.objects.filter(
        user=user,
        state__in=[RemoteSession.State.PENDING, RemoteSession.State.OPEN]).count()


@override_settings(VIGIL_SESSION_BUS="memory")
class CloseWithoutASocketTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user("owner")
        self.host = make_host("no-socket")
        self.client.force_authenticate(self.user)

    def test_closing_from_the_dashboard_ends_a_row_nobody_is_attached_to(self):
        session = make_session(self.user, self.host, RemoteSession.State.OPEN)

        resp = self.client.post(f"/api/v1/sessions/{session.id}/close/")

        self.assertEqual(resp.status_code, 202, resp.content)
        session.refresh_from_db()
        self.assertEqual(session.state, RemoteSession.State.CLOSED)
        self.assertIsNotNone(session.closed_at)
        self.assertEqual(session.close_reason, "closed by owner")

    def test_a_closed_row_is_closed_once_and_the_end_audit_does_not_double(self):
        session = make_session(self.user, self.host, RemoteSession.State.OPEN)

        self.client.post(f"/api/v1/sessions/{session.id}/close/")
        self.client.post(f"/api/v1/sessions/{session.id}/close/")

        # patch() on the module attribute: _audit_end imports `record` at call
        # time, and a swallowed write must not turn this into a false pass.
        with mock.patch("apps_business.audits.models.record") as written:
            self.client.post(f"/api/v1/sessions/{session.id}/close/")
        self.assertEqual(written.call_count, 0)

    def test_three_dead_rows_stop_a_fourth_session_until_one_is_closed(self):
        sessions = [make_session(self.user, self.host, RemoteSession.State.OPEN)
                    for _ in range(3)]

        self.assertEqual(active_count(self.user), 3)
        resp = self.client.post(f"/api/v1/sessions/{sessions[0].id}/close/")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(active_count(self.user), 2)


class FinishIdempotentTests(TestCase):
    def setUp(self):
        self.user = make_user("twice")
        self.session = make_session(self.user, make_host("finish-twice"),
                                    RemoteSession.State.OPEN)

    def test_the_second_close_changes_nothing(self):
        self.assertTrue(_finish(self.session, "closed", "browser closed"))
        self.session.refresh_from_db()
        first_closed_at = self.session.closed_at

        self.assertFalse(_finish(self.session, "expired", "the agent did not answer"))
        self.session.refresh_from_db()
        self.assertEqual(self.session.close_reason, "browser closed")
        self.assertEqual(self.session.state, RemoteSession.State.CLOSED)
        self.assertEqual(self.session.closed_at, first_closed_at)

class SweepStaleTests(TestCase):
    def setUp(self):
        self.user = make_user("swept")
        self.host = make_host("sweepable")
        # The sweep runs at a fixed instant with the thresholds it ships with,
        # so a test moves its clock instead of patching module constants.
        self.now = now()

    def _sweep(self) -> int:
        return tasks._sweep(self.now, tasks.HEARTBEAT_STALE_SECONDS,
                            tasks.ANSWER_GRACE_SECONDS, tasks.HARD_LIMIT_GRACE_SECONDS)

    def _session(self, state, **ages) -> RemoteSession:
        return make_session(self.user, self.host, state, moment=self.now, **ages)

    def test_the_sweep_closes_the_three_kinds_of_dead_and_touches_nothing_alive(self):
        pending = RemoteSession.State.PENDING
        open_ = RemoteSession.State.OPEN
        # label -> (how the row is aged, does the sweep close it, with what,
        #           the state it starts in, the state it ends in)
        fixtures = {
            "pending, agent never answered": (
                {"opened": None, "seen": None, "created": timedelta(minutes=10)},
                True, "the agent did not answer", pending, RemoteSession.State.EXPIRED),
            "pending, still within the answer window": (
                {"opened": None, "seen": None, "created": timedelta(seconds=10)},
                False, "", pending, pending),
            "open, heartbeat five minutes old": (
                {"opened": timedelta(minutes=10), "seen": timedelta(minutes=5)},
                True, "connection lost", open_, RemoteSession.State.CLOSED),
            "open, heartbeat thirty seconds old": (
                {"seen": timedelta(seconds=30)},
                False, "", open_, open_),
            "open, row that predates the heartbeat": (
                {"opened": timedelta(minutes=5), "seen": None},
                True, "connection lost", open_, RemoteSession.State.CLOSED),
        }

        rows = {label: self._session(state, **ages)
                for label, (ages, _closed, _reason, state, _end) in fixtures.items()}

        closed = self._sweep()

        self.assertEqual(closed, 3)
        for label, row in rows.items():
            _ages, was_closed, reason, _start, end_state = fixtures[label]
            row.refresh_from_db()
            self.assertEqual(row.close_reason, reason, label)
            self.assertEqual(row.state, end_state, label)
            self.assertEqual(row.closed_at is not None, was_closed, label)

    def test_a_session_past_its_hard_limit_closes_with_the_time_limit_reason(self):
        session = self._session(RemoteSession.State.OPEN, opened=timedelta(hours=9))

        self.assertEqual(self._sweep(), 1)
        session.refresh_from_db()
        self.assertEqual(session.state, RemoteSession.State.CLOSED)
        self.assertEqual(session.close_reason, "time limit")

    def test_a_session_the_sweep_leaves_alone_is_untouched(self):
        alive = self._session(RemoteSession.State.OPEN)
        seen = alive.last_seen

        self.assertEqual(self._sweep(), 0)
        alive.refresh_from_db()
        self.assertEqual(alive.state, RemoteSession.State.OPEN)
        self.assertEqual(alive.last_seen, seen)


@override_settings(VIGIL_SESSION_BUS="memory")
class TeardownAfterADashboardCloseTests(TestCase):
    def test_the_socket_still_writes_its_byte_totals_after_someone_else_closed_the_row(self):
        from ..ws import _persist_bytes

        session = make_session(make_user("bytes"), make_host("bytes-host"),
                               RemoteSession.State.OPEN)
        self.assertTrue(_finish(session, "closed", "closed by an admin"))
        session.bytes_in, session.bytes_out = 12, 34
        # The socket's teardown: its own close loses the race, its totals do not.
        self.assertFalse(_finish(session, "closed", "the browser went away"))
        _persist_bytes(session)

        session.refresh_from_db()
        self.assertEqual((session.bytes_in, session.bytes_out), (12, 34))
        self.assertEqual(session.close_reason, "closed by an admin")
