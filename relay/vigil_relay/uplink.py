"""The uplink: one WebSocket per tenant, held open by the dashboard's link.

Frames relay → link: ``req``, ``ws_open``, ``ws_data``, ``ws_close``, ``ping``.
Frames link → relay: ``res``, ``ws_data``, ``ws_close``, ``pong``. Nothing here
logs a frame body — request bodies exist only in flight.
"""

import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import web


class UplinkDown(Exception):
    """The uplink is not connected, or dropped while a request was in flight."""


@dataclass
class Uplink:
    slug: str
    ws: "web.WebSocketResponse | None" = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    pipes: dict[str, web.WebSocketResponse] = field(default_factory=dict)
    last_pong: float = field(default_factory=time.monotonic)

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def send(self, frame: dict) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            raise UplinkDown(f"uplink for {self.slug} is down")
        await ws.send_json(frame)

    async def request(self, frame: dict, timeout: float) -> dict:
        """Send a request frame and wait for the matching ``res``. The caller's
        frame is mutated with its id so replay can identify it."""
        if not self.connected:
            raise UplinkDown(f"uplink for {self.slug} is down")
        rid = uuid.uuid4().hex
        frame["id"] = rid
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending[rid] = fut
        try:
            await self.send(frame)
        except UplinkDown:
            self.pending.pop(rid, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise
        except asyncio.CancelledError:
            self.pending.pop(rid, None)
            raise

    async def handle(self, msg: dict) -> None:
        """Dispatch one inbound link frame."""
        kind = msg.get("t")
        if kind == "res":
            fut = self.pending.pop(str(msg.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "ws_data":
            pipe = self.pipes.get(str(msg.get("id")))
            if pipe is not None and not pipe.closed:
                # One agent socket going away mid-send must not take the
                # uplink, and every other agent on it, down with it.
                try:
                    if msg.get("bin"):
                        await pipe.send_bytes(base64.b64decode(msg.get("data", "")))
                    else:
                        await pipe.send_str(str(msg.get("data", "")))
                except (ConnectionResetError, RuntimeError, ValueError):
                    self.pipes.pop(str(msg.get("id")), None)
        elif kind == "ws_close":
            pipe = self.pipes.pop(str(msg.get("id")), None)
            if pipe is not None and not pipe.closed:
                await pipe.close()
        elif kind == "pong":
            self.last_pong = time.monotonic()

    def fail_pending(self, exc: Exception) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()
