"""A server restart leaves no socket alive, so it closes every open session."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from ..models import RemoteSession
from .test_stale_sessions import make_host, make_session, make_user


class CloseOnStartTests(TestCase):
    def test_every_pending_and_open_row_is_closed(self):
        user = make_user("op")
        host = make_host("web1")
        pending = make_session(user, host, RemoteSession.State.PENDING)
        opened = make_session(user, host, RemoteSession.State.OPEN)
        done = make_session(user, host, RemoteSession.State.CLOSED)

        out = StringIO()
        call_command("close_open_sessions", stdout=out)

        for row in (pending, opened):
            row.refresh_from_db()
            self.assertEqual(row.state, RemoteSession.State.CLOSED)
            self.assertEqual(row.close_reason, "server restarted")
        done.refresh_from_db()
        self.assertEqual(done.close_reason, "")
        self.assertIn("Closed 2", out.getvalue())

    def test_the_web_entrypoint_runs_it_before_gunicorn(self):
        from pathlib import Path

        script = (Path(__file__).resolve().parents[3] / "entrypoint.sh").read_text()
        self.assertIn("close_open_sessions", script)
