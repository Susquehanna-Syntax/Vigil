"""Close every PENDING/OPEN remote session: run by the web container as it starts.

A session's only live end is a websocket held by one web worker. When the web
container starts, no worker from before is alive, so every row still PENDING or
OPEN belongs to a socket that died with the old process. Left alone, each one
holds one of the user's session slots until the beat sweep notices its heartbeat
stopped; closing them here means a restart frees every slot at once.
"""

from django.core.management.base import BaseCommand

from apps.remote.models import RemoteSession
from apps.remote.ws import _close_now


class Command(BaseCommand):
    help = "Close the remote sessions a restarted server can no longer be holding."

    def handle(self, *args, **options):
        closed = 0
        rows = RemoteSession.objects.filter(
            state__in=[RemoteSession.State.PENDING, RemoteSession.State.OPEN],
        ).select_related("host", "user")
        for session in list(rows):
            if _close_now(session, RemoteSession.State.CLOSED, "server restarted"):
                closed += 1
        self.stdout.write(f"Closed {closed} remote session(s) left open by the last run.")
