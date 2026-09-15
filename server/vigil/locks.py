"""One-at-a-time execution for periodic tasks.

Vigil's beats guard themselves with state — "is there already a firing alert
for this rule and host", "has this host already got a pending run" — which is
correct for the sequential case and does nothing for the concurrent one. Two
overlapping passes both read the state before either writes, and both act.

That is not hypothetical: several beats run on a 60-second schedule over a
host × rule inner loop, so a pass that slows down under load overlaps its own
successor exactly when the fleet is busiest and duplicates matter most.

PostgreSQL advisory locks are the right tool — they are held by the session,
released automatically if the worker dies, and cost one round trip. On SQLite
(local dev, the test suite) there is no equivalent and no concurrency to
protect against, so the lock is a no-op that always succeeds.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

from django.db import connection


def _key(name: str) -> int:
    """A stable 63-bit integer for *name*, which is what pg_advisory_lock takes."""
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


@contextmanager
def advisory_lock(name: str):
    """Yield True when this process holds *name*, False when someone else does.

    Never blocks: a beat that cannot get the lock has nothing to wait for,
    because whoever holds it is already doing the same sweep. Skipping is the
    correct outcome and the caller says so in its log.
    """
    if connection.vendor != "postgresql":
        # SQLite has no advisory locks, and no concurrent workers either.
        yield True
        return

    key = _key(name)
    acquired = False
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
            acquired = bool(cur.fetchone()[0])
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [key])
