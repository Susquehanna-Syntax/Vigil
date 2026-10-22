"""The session bus: a few keys, two implementations.

A session spans processes that know nothing about each other: the API request
that creates it, the agent's long-poll that fetches its ticket, and the two
WebSocket tasks that pipe bytes. The bus is how they hand messages over.

``VIGIL_SESSION_BUS`` chooses:

* ``redis`` (the default): multi-process. gunicorn runs several Uvicorn
  workers, and a browser socket in one worker has to reach an agent socket in
  another.
* ``memory``: a single-process stand-in for tests and ``runserver``. It holds
  queues in this interpreter only, so two workers would each have their own
  copy of every key. Never select it in a deployment with more than one.
"""

import asyncio
import collections
import json
import threading
import time

from django.conf import settings

#: Tickets waiting for an agent to wake up and fetch them.
WAKE_KEY = "vigil:wake:{host_id}"
#: browser → agent, and agent → browser, per session.
B2A_KEY = "vigil:sess:{sid}:b2a"
A2B_KEY = "vigil:sess:{sid}:a2b"
#: The claims on a session's one browser socket and one agent socket.
BROWSER_KEY = "vigil:sess:{sid}:browser"
AGENT_KEY = "vigil:sess:{sid}:agent"

#: An unread key is swept after this long. A wake ticket is useless 60 s after
#: it was signed; a session key outliving its session is only a leak.
KEY_TTL_SECONDS = 600

#: How often a memory-bus pop looks again. Tests and runserver only.
_POLL_SECONDS = 0.01


def _backend() -> str:
    return getattr(settings, "VIGIL_SESSION_BUS", "redis")


class MemoryBus:
    """In-process bus for tests and ``runserver``: single-process only.

    Views push from whatever thread the server runs them in, and pops wait in
    an event loop, so the queues sit behind a plain lock and a pop polls rather
    than sleeping on a loop-bound primitive another thread cannot wake.
    """

    def __init__(self) -> None:
        self._queues: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def push_sync(self, key: str, item: dict) -> None:
        with self._lock:
            self._queues[key].append(item)

    async def push(self, key: str, item: dict) -> None:
        self.push_sync(key, item)

    def _take(self, key: str) -> dict | None:
        with self._lock:
            queue = self._queues.get(key)
            return queue.popleft() if queue else None

    async def pop(self, key: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            item = self._take(key)
            if item is not None or time.monotonic() >= deadline:
                return item
            await asyncio.sleep(_POLL_SECONDS)

    async def drain(self, key: str) -> list[dict]:
        with self._lock:
            queue = self._queues.pop(key, None)
        return list(queue or [])

    async def claim_browser(self, key: str) -> bool:
        with self._lock:
            if self._queues.get(key):
                return False
            self._queues[key].append({"claimed": True})
            return True

    def peek(self, key: str) -> list[dict]:
        """What is waiting on *key*, without taking it. Tests only."""
        with self._lock:
            return list(self._queues.get(key) or [])

    def reset(self) -> None:
        """Forget every key, so test cases do not inherit each other's items."""
        with self._lock:
            self._queues.clear()


class RedisBus:
    """The deployment bus: one Redis list per key, shared by every worker."""

    def __init__(self) -> None:
        self._async = None
        self._async_loop = None
        self._sync = None

    def _client(self):
        # One client per event loop. A Uvicorn worker runs a single loop, so in
        # a deployment this is one client per worker; but an async view reached
        # through async_to_sync gets a fresh loop per call, and a pool made on a
        # loop that has since closed fails with "Event loop is closed".
        loop = asyncio.get_running_loop()
        if self._async is None or self._async_loop is not loop:
            import redis.asyncio

            self._async = redis.asyncio.from_url(settings.CELERY_BROKER_URL)
            self._async_loop = loop
        return self._async

    def _sync_client(self):
        if self._sync is None:
            import redis

            self._sync = redis.from_url(settings.CELERY_BROKER_URL)
        return self._sync

    async def push(self, key: str, item: dict) -> None:
        client = self._client()
        await client.rpush(key, json.dumps(item))
        await client.expire(key, KEY_TTL_SECONDS)

    def push_sync(self, key: str, item: dict) -> None:
        client = self._sync_client()
        client.rpush(key, json.dumps(item))
        client.expire(key, KEY_TTL_SECONDS)

    async def pop(self, key: str, timeout: float) -> dict | None:
        # BLPOP takes whole-or-fractional seconds; 0 would block forever.
        found = await self._client().blpop([key], timeout=max(timeout, 0.01))
        return json.loads(found[1]) if found else None

    async def drain(self, key: str) -> list[dict]:
        client = self._client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            raw, _deleted = await pipe.execute()
        return [json.loads(item) for item in raw]

    async def claim_browser(self, key: str) -> bool:
        return bool(await self._client().set(key, "1", nx=True, ex=KEY_TTL_SECONDS))


_memory = MemoryBus()
_redis = RedisBus()


def _bus():
    return _memory if _backend() == "memory" else _redis


def memory_bus() -> MemoryBus:
    """The in-process bus, so tests can seed, peek and reset it."""
    return _memory


async def push(key: str, item: dict) -> None:
    await _bus().push(key, item)


async def pop(key: str, timeout: float) -> dict | None:
    return await _bus().pop(key, timeout)


def push_sync(key: str, item: dict) -> None:
    _bus().push_sync(key, item)


async def drain(key: str) -> list[dict]:
    return await _bus().drain(key)


async def claim_browser(key: str) -> bool:
    """True for the first claim on *key*, False for any later one. Used for a
    session's browser socket and its agent socket alike."""
    return await _bus().claim_browser(key)


# Test helpers. They work on the memory bus only, which every test selects.

def enqueue(key: str, item: dict) -> None:
    """Seed *key* with *item*."""
    _memory.push_sync(key, item)


def peek(key: str) -> list[dict]:
    """What is waiting on *key*, without taking it."""
    return _memory.peek(key)


def reset() -> None:
    """Forget every key on the memory bus."""
    _memory.reset()
