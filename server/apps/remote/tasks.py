"""Beat's sweep of remote sessions nobody is attached to anymore.

A row only leaves PENDING/OPEN when the browser socket tears down, so a tab
closed during a restart or a worker killed mid-session would hold one of the
user's three session slots forever. The socket's heartbeat (``last_seen``) says
who is really gone. ``_sweep`` takes its clock and its thresholds as arguments:
reading the module constants at call time would let a test age a row past a
limit by moving nothing at all, which is exactly what it has to prove wrong.
"""
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils.timezone import now

from vigil import scoping

from .models import RemoteSession
from .ws import AGENT_ANSWER_SECONDS, HARD_LIMIT_SECONDS, _close_now

#: The watchdog writes the heartbeat once a minute, so three missed beats means
#: no socket is attached any more.
HEARTBEAT_STALE_SECONDS = 180
#: Grace on top of each socket-side deadline: the watchdog should have closed
#: the row itself; the sweep only picks up what it could not.
ANSWER_GRACE_SECONDS = 60
HARD_LIMIT_GRACE_SECONDS = 300


@shared_task(name="remote.sweep_stale")
@scoping.system_job(reason="remote session sweep: closes stale sessions on every host")
def sweep_stale() -> int:
    """Close the PENDING/OPEN rows no live socket can close any more. Returns
    how many rows it closed."""
    return _sweep(now(), HEARTBEAT_STALE_SECONDS, ANSWER_GRACE_SECONDS,
                  HARD_LIMIT_GRACE_SECONDS)


def _sweep(moment, heartbeat_age: int, answer_grace: int, hard_grace: int) -> int:
    """The sweep at a given instant. The clock is an argument because a test of
    "older than the limit" must move the limit's measure as well as the row.
    Each pass materialises its ids first: closing rows while the same query is
    being streamed would have later passes read state a previous pass wrote."""
    expired = [session.pk for session in _stale_pending(
        moment, AGENT_ANSWER_SECONDS + answer_grace)]
    lost = [session.pk for session in _lost_heartbeat(moment, heartbeat_age)]
    overrun = _overrun(moment, HARD_LIMIT_SECONDS + hard_grace)
    return sum([
        # Nobody ever answered it, and the socket that would have given up on it
        # died without expiring the row.
        _close_matching(RemoteSession.objects.filter(
            pk__in=expired, state=RemoteSession.State.PENDING),
            "expired", "the agent did not answer"),
        # Attached once, then silent: either the heartbeat stopped or it never
        # started, because the row predates the heartbeat. The earlier close
        # wins its rows by the state guard inside _finish.
        _close_matching(RemoteSession.objects.filter(
            pk__in=lost, state=RemoteSession.State.OPEN),
            "closed", "connection lost"),
        # Past the socket's own hard limit plus grace: its watchdog died with it.
        _close_matching(overrun, "closed", "time limit"),
    ])


def _stale_pending(moment, answer_age: int):
    return RemoteSession.objects.filter(
        state=RemoteSession.State.PENDING,
        created_at__lt=moment - timedelta(seconds=answer_age))


def _lost_heartbeat(moment, heartbeat_age: int):
    stale = moment - timedelta(seconds=heartbeat_age)
    return RemoteSession.objects.filter(
        state=RemoteSession.State.OPEN, opened_at__lt=stale,
    ).filter(Q(last_seen__isnull=True) | Q(last_seen__lt=stale))


def _overrun(moment, hard_age: int):
    return RemoteSession.objects.filter(
        state=RemoteSession.State.OPEN,
        opened_at__lt=moment - timedelta(seconds=hard_age))


def _close_matching(sessions, state: str, reason: str) -> int:
    closed = 0
    for session in list(sessions.select_related("host", "user")):
        if _close_now(session, state, reason):
            closed += 1
    return closed
