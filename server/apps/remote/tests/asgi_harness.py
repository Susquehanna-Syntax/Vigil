"""Drives an ASGI WebSocket application without a server in the way.

A live session is three coroutines talking over sockets, so the only honest way
to test one is to be the peer: feed it ``websocket.connect``, answer what it
sends, and read the close code it settles on. Wrapping that in two queues rather
than a browser keeps the tests hermetic — no ports, no threads, and a refused
upgrade fails the test instead of hanging it.

Nothing here waits on the application task except in ``settle``, because an app
that hangs should fail with a timeout rather than wedge the suite.
"""

import asyncio
import json


class Conn:
    """One WebSocket connection to an ASGI app, seen from outside it.

    The wire records every event the app sends instead of handing them over once,
    so a close frame cannot be missed by a test that was busy awaiting something
    else at the moment it arrived.
    """

    def __init__(self, path: str, headers: list[tuple[bytes, bytes]],
                 task: asyncio.Task, wire: "Wire"):
        self.path = path
        self.headers = headers
        self._task = task
        self._wire = wire
        #: How far through what the app has sent we have already read.
        self._read = 0

    async def send_json(self, payload: dict) -> None:
        await self._wire.to_app.put({"type": "websocket.receive",
                                     "text": json.dumps(payload)})

    async def receive_json(self, timeout: float = 5.0) -> dict | None:
        """The next text frame the app sends after the one we last read."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            index = self._next_text()
            if index is not None:
                self._read = index + 1
                return json.loads(self._wire.sent[index].get("text") or "{}")
            if self._closed_at() is not None:
                # It refused us, or it was already closing: no frame is coming.
                return None
            # Cleared before waiting: a set event returns at once, and a reader
            # that never suspends starves the app it is waiting on.
            self._wire.moved.clear()
            try:
                await asyncio.wait_for(self._wire.moved.wait(),
                                       max(0.01, deadline - loop.time()))
            except (asyncio.TimeoutError, TimeoutError):
                return None

    async def drain_to_close(self, timeout: float = 5.0) -> dict | None:
        """Read on until the app closes, and hand back the last frame it said."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: dict | None = None
        while self.closed_code is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            frame = await self.receive_json(remaining)
            if frame is None:
                break
            last = frame
        return last

    async def answered(self, timeout: float = 5.0) -> bool:
        """Wait until the app accepts or closes; True when it accepted."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.accepted and self.closed_code is None and not self._task.done():
            self._wire.moved.clear()
            try:
                await asyncio.wait_for(self._wire.moved.wait(),
                                       max(0.01, deadline - loop.time()))
            except (asyncio.TimeoutError, TimeoutError):
                break
        return self.accepted

    @property
    def accepted(self) -> bool:
        """Whether the app sent ``websocket.accept``."""
        return any(event.get("type") == "websocket.accept" for event in self._wire.sent)

    @property
    def closed_code(self) -> int | None:
        """The code the app closed with, once it has sent one."""
        index = self._closed_at()
        return self._wire.sent[index].get("code") if index is not None else None

    async def close(self) -> None:
        """Hang up the way a browser does: ``websocket.disconnect``, then wait."""
        await self._wire.to_app.put({"type": "websocket.disconnect", "code": 1000})
        await self.settle()

    async def settle(self, timeout: float = 5.0) -> None:
        """Let the app finish its exit path — its close frame, its audit rows."""
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _next_text(self) -> int | None:
        for index in range(self._read, len(self._wire.sent)):
            if self._wire.sent[index].get("type") == "websocket.send":
                return index
        return None

    def _closed_at(self) -> int | None:
        """The first close frame at or after what we have read: a close ends the
        conversation, so frames behind it are history rather than news."""
        start = self._read if self._read else 0
        for index in range(start, len(self._wire.sent)):
            if self._wire.sent[index].get("type") == "websocket.close":
                return index
        return None


class Wire:
    """The two queues an ASGI app sees, as the peer behind them."""

    def __init__(self, path: str, headers: list[tuple[bytes, bytes]]):
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": list(headers),
            "client": ("203.0.113.7", 51000),
            "server": ("vigil.test", 8000),
            "subprotocols": [],
        }
        #: What we hand the app when it calls ``receive``.
        self.to_app: asyncio.Queue = asyncio.Queue()
        #: Everything the app has passed to ``send``, in order.
        self.sent: list[dict] = []
        #: Signalled whenever ``sent`` grows, so a reader can wait for it.
        self.moved = asyncio.Event()

    async def receive(self) -> dict:
        return await self.to_app.get()

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        self.moved.set()


async def connect(app, path: str, cookie: str = "",
                  origin: str = "http://testserver",
                  headers: list[tuple[bytes, bytes]] | None = None) -> Conn:
    """Open a session socket to *app* and hand back its peer.

    ``cookie`` and ``origin`` are the two headers that decide whose session this
    is and whether the browser asking for it counts as one of ours, so they are
    parameters rather than something each caller assembles by hand.
    """
    wire = Wire(path, list(headers or []) + _default_headers(cookie, origin))
    await wire.to_app.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(wire.scope, wire.receive, wire.send))
    return Conn(path, wire.scope["headers"], task, wire)


def _default_headers(cookie: str, origin: str) -> list[tuple[bytes, bytes]]:
    # The test client's host: the Origin check compares the two.
    headers = [(b"host", b"testserver")]
    if origin:
        headers.append((b"origin", origin.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return headers
