"""The dashboard side of a relay: one outbound WebSocket per enabled remote relay.

``manage.py relay_link`` runs :class:`LinkManager` for the lifetime of the
process. Each uplink is held open by us — the relay never dials in — so the
dashboard needs no inbound port for relays to work.

Everything the relay forwards is an agent request, and every one of them is
stamped with our own attestation (:func:`forward_headers`): the relay cannot
claim an agent's address, and neither can the agent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass

import aiohttp
from django.utils.timezone import now

from apps.hosts.crypto import decrypt_secret

from .models import Relay
from .trust import is_agent_path, link_token

UPSTREAM = os.environ.get("VIGIL_LINK_UPSTREAM", "http://web:8000")
HOST_HEADER = os.environ.get("VIGIL_LINK_HOST", "localhost")
RELOAD_SECONDS = 15
STATUS_SECONDS = 30
REQUEST_TIMEOUT = 55

MAX_MSG_SIZE = 32 * 1024 * 1024
BACKOFF_CEILING = 30
HEALTHY_SECONDS = 60

# Everything we refuse to forward from a relay: hop-by-hop headers aiohttp must
# own, and the attestation space that belongs to us alone.
DROP_HEADERS = frozenset((
    "x-vigil-link", "x-vigil-relay", "x-vigil-agent-ip",
    "host", "content-length", "connection", "upgrade", "transfer-encoding",
))
# aiohttp sets Content-Length itself; a stale one from Django corrupts the reply.
DROP_RESPONSE_HEADERS = frozenset(("content-length", "transfer-encoding", "connection"))

#: A 502 carries this verbatim, so the test and the agent see the same string.
UNREACHABLE = b'{"error": "Vigil web service unreachable"}'

log = logging.getLogger(__name__)


def _is_vigil(name: str) -> bool:
    return name.lower().startswith("x-vigil-")


@dataclass(frozen=True)
class Target:
    id: str
    url: str
    key: str
    key_sha256: str


def load_targets() -> list[Target]:
    """Every enabled remote relay the link can actually speak to."""
    targets = []
    for relay in Relay.objects.filter(kind=Relay.Kind.REMOTE, enabled=True).exclude(url=""):
        key = decrypt_secret(relay.key_encrypted)
        if not key:
            continue
        targets.append(Target(
            id=str(relay.id),
            url=relay.url.rstrip("/"),
            key=key,
            key_sha256=relay.key_sha256,
        ))
    return targets


def record_status(relay_id: str, *, connected: bool, error: str = "") -> None:
    """Write the connection bookkeeping the UI shows.

    ``.update()`` so a relay the admin renamed mid-flight keeps its edits. A
    failed attempt without a reason (the socket just closed) must not clear
    ``last_error`` — that is the only thing telling an admin why it is down.
    """
    fields = {"connected": connected}
    if connected:
        fields["last_seen_at"] = now()
        fields["last_error"] = ""
    elif error:
        fields["last_error"] = error[:300]
    Relay.objects.filter(pk=relay_id).update(**fields)


_METHODS = ("GET", "POST", "HEAD")


def forwardable(method: str, path: str) -> bool:
    """Only agent paths cross the link, checked here as well as at the relay.

    The relay is a separate machine, possibly hosted by someone else: if it is
    compromised it must still not be able to reach the login page or the API
    through us. The path is also concatenated onto UPSTREAM, so it has to be a
    plain absolute path — "@evil.example/..." would otherwise change the host.
    """
    return (method.upper() in _METHODS and path.startswith("/") and not path.startswith("//")
            and ".." not in path and "@" not in path and is_agent_path(path))


def pipeable(path: str) -> bool:
    return path.startswith("/ws/agent/") and ".." not in path and "@" not in path


def forward_headers(frame_headers: dict, relay_id: str, ip: str) -> dict:
    """The headers to send upstream: the agent's, plus our attestation.

    The order is the security property — the copy loop drops every ``X-Vigil-*``
    the agent wrote, and ours are written after it, so a forged one cannot win.
    """
    out = {}
    for name, value in (frame_headers or {}).items():
        # The whole X-Vigil-* namespace is ours, including names we have not
        # invented yet: an agent must not be able to put words in it.
        if name.lower() in DROP_HEADERS or _is_vigil(name):
            continue
        out[name] = value
    out["X-Vigil-Link"] = link_token()
    out["X-Vigil-Relay"] = relay_id
    out["X-Vigil-Agent-IP"] = ip
    out["Host"] = HOST_HEADER
    return out


class _Pipe:
    """One agent WebSocket, bridged to the dashboard.

    ``open`` separates "the relay still has an agent on this end" from "this
    socket happens to be closed": when the link drops we close our side silently,
    and telling the relay ws_close would report an agent that never left as gone.
    """

    __slots__ = ("open", "ws")

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self.ws = ws
        self.open = True

    async def deliver(self, frame: dict) -> None:
        if frame.get("bin"):
            await self.ws.send_bytes(_b64decode(frame.get("data")))
        else:
            await self.ws.send_str(str(frame.get("data", "")))

    async def aclose(self) -> None:
        self.open = False
        if not self.ws.closed:
            await self.ws.close()


class Uplink:
    """One relay. Reconnects forever; forwards while connected."""

    def __init__(self, target: Target, *, upstream: str = UPSTREAM,
                 status=record_status, session_factory=aiohttp.ClientSession):
        self.target = target
        self.upstream = upstream.rstrip("/")
        self._status = status
        self._session_factory = session_factory
        self.session: aiohttp.ClientSession | None = None
        self.ws = None
        self.pipes: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._tasks: set = set()
        self._dropping = False

    async def run(self) -> None:
        """Reconnect forever. Cancellation is the manager's way of retiring this
        relay, and it unwinds straight out of ``_session`` — so the sockets that
        call owns are closed here, on the only path that sees it."""
        delay = 1
        while True:
            self._dropping = False
            started = asyncio.get_running_loop().time()
            try:
                await self._session()
            except asyncio.CancelledError:
                self._dropping = True
                await self._drop()
                # A task can be cancelled while we are unwinding and still reach
                # its done-callback after gather returns; two passes is enough.
                for _ in range(2):
                    if not self._tasks:
                        break
                    await asyncio.gather(*list(self._tasks), return_exceptions=True)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError,
                    UnicodeDecodeError, ValueError) as exc:
                # A refused key is worth naming: it is the one failure an admin
                # fixes by rotating the key rather than by restarting the link.
                await self._report(connected=False, error=_reason(exc))
                # Back off only from a failure to stay connected; a link that held
                # for a while has earned its fast retry back.
                delay = 1 if asyncio.get_running_loop().time() - started > HEALTHY_SECONDS \
                    else min(delay * 2, BACKOFF_CEILING)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        async with self._session_factory() as session:
            self.session = session
            async with session.ws_connect(
                f"{self.target.url}/relay/uplink",
                headers={"Authorization": f"Bearer {self.target.key}"},
                heartbeat=None,
                max_msg_size=MAX_MSG_SIZE,
            ) as ws:
                self.ws = ws
                await self._report(connected=True)
                beat = asyncio.create_task(self._beat())
                try:
                    # One frame at a time, and nothing here waits on an agent's
                    # round trip: _dispatch only ever fans work out to tasks.
                    async for msg in ws:
                        # Anything but TEXT (close, closing, error, binary) is the
                        # relay hanging up; there is nothing else to do with it.
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            break
                        await self._dispatch(msg)
                finally:
                    beat.cancel()
                    await self._drop()
                # Only a clean close lands here: a cancellation propagates out of
                # the read loop instead, and a connection we never made raised.
                await self._report(connected=False, error="relay closed the uplink")

    async def _beat(self) -> None:
        """Keep ``last_seen_at`` fresh while the socket is up but idle."""
        while True:
            await asyncio.sleep(STATUS_SECONDS)
            await self._report(connected=True)

    async def _drop(self) -> None:
        """Close everything this uplink owns: the agent pipes first, so no
        reader task outlives the socket it writes to."""
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        for pipe in list(self.pipes.values()):
            await pipe.aclose()
        self.pipes.clear()

    # ------------------------------------------------------------ dispatch

    async def _dispatch(self, msg) -> None:
        """One inbound frame. Nothing here waits on an agent's round trip — a slow
        request or upstream connect must not stall the uplink — so both fan out to
        tasks and this returns without awaiting them. Ordering survives because
        ``ws_open`` registers its pipe before returning, and every reply is sent
        from the task that produced it, in the order those tasks were started."""
        try:
            frame = json.loads(msg.data)
        except (ValueError, TypeError):
            return
        if not isinstance(frame, dict):
            return

        kind = frame.get("t")
        if kind == "ping":
            await self._send({"t": "pong"})
        elif kind == "req":
            self._keep(asyncio.create_task(self._forward(frame)))
        elif kind == "ws_open":
            await self._open_pipe(frame)
        elif kind == "ws_data":
            await self._pipe_data(frame)
        elif kind == "ws_close":
            await self._close_pipe(str(frame.get("id", "")))

    # ---------------------------------------------------------- forwarding

    async def _forward(self, frame: dict) -> None:
        rid = frame.get("id")
        path = str(frame.get("path", ""))
        query = str(frame.get("query", ""))
        if not forwardable(str(frame.get("method", "GET")), path) or "#" in query:
            await self._send({"t": "res", "id": rid, "status": 404, "headers": {},
                              "body": _b64encode(b'{"error": "not found"}')})
            return
        try:
            resp = await self.session.request(
                str(frame.get("method", "GET")),
                self.upstream + path + (("?" + query) if query else ""),
                headers=forward_headers(frame.get("headers") or {}, self.target.id,
                                        str(frame.get("ip", ""))),
                data=_b64decode(frame.get("body")),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            await self._send({"t": "res", "id": rid, "status": 502, "headers": {},
                              "body": _b64encode(UNREACHABLE)})
            return

        async with resp:
            body = await resp.read()
            headers = {name: value for name, value in resp.headers.items()
                       if name.lower() not in DROP_RESPONSE_HEADERS}
        # A reply for every req id, always — the relay parks the agent's request
        # on this frame and nothing else will wake it.
        await self._send({"t": "res", "id": rid, "status": resp.status,
                          "headers": headers, "body": _b64encode(body)})

    # --------------------------------------------------------------- pipes

    async def _open_pipe(self, frame: dict) -> None:
        """Connect upstream and register the pipe. Returns as soon as the pipe can
        carry data; reading it is left to a task."""
        pid = str(frame.get("id", ""))
        if not pipeable(str(frame.get("path", ""))) or "#" in str(frame.get("query", "")):
            await self._send({"t": "ws_close", "id": pid})
            return
        try:
            upstream = await self.session.ws_connect(
                self._pipe_url(frame),
                headers=forward_headers(frame.get("headers") or {}, self.target.id,
                                        str(frame.get("ip", ""))),
                heartbeat=None,
                max_msg_size=MAX_MSG_SIZE,
            )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, ValueError):
            await self._send({"t": "ws_close", "id": pid})
            return

        pipe = _Pipe(upstream)
        # Before anything is awaited again: frames arrive in order after this one,
        # and the first of them must find its socket already registered here.
        self.pipes[pid] = pipe
        try:
            self._keep(asyncio.create_task(self._pump(pid, pipe)))
        except asyncio.CancelledError:
            await upstream.close()
            self.pipes.pop(pid, None)
            raise

    async def _pump(self, pid: str, pipe: _Pipe) -> None:
        """Upstream → relay. When the dashboard closes the socket the agent's pipe
        closes too, so the agent never hangs on a half-open connection."""
        try:
            while True:
                msg = await pipe.ws.receive()
                # Checked per message rather than once around the loop: this is a
                # blocking read, so without it a frame that arrived just before we
                # put the pipe down would still reach the relay as ws_data — for a
                # pipe the relay may already have forgotten.
                if not pipe.open:
                    break
                if msg.type is aiohttp.WSMsgType.TEXT:
                    await self._send({"t": "ws_data", "id": pid, "data": _text(msg.data)})
                elif msg.type is aiohttp.WSMsgType.BINARY:
                    await self._send({"t": "ws_data", "id": pid,
                                      "data": _b64encode(msg.data), "bin": True})
                else:
                    break
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, RuntimeError):
            pass
        finally:
            if self.pipes.get(pid) is pipe:
                self.pipes.pop(pid, None)
            await pipe.aclose()
        # Only the relay's own ws_close, or this read loop ending on its own,
        # puts a pipe down — never the link tearing it down, which would tell the
        # relay an agent hung up when in fact it was our socket that went.
        await self._send({"t": "ws_close", "id": pid})

    async def _pipe_data(self, frame: dict) -> None:
        pid = str(frame.get("id", ""))
        pipe = self.pipes.get(pid)
        if pipe is None or not pipe.open:
            return
        try:
            await pipe.deliver(frame)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            self.pipes.pop(pid, None)

    async def _close_pipe(self, pid: str) -> None:
        """The relay's own ws_close — the one thing that may tell the agent its
        side of the pipe is gone."""
        pipe = self.pipes.pop(pid, None)
        if pipe is not None:
            await pipe.aclose()

    # ------------------------------------------------------------- helpers

    def _pipe_url(self, frame: dict) -> str:
        """Upstream address for an agent WebSocket. ``http`` → ``ws`` and the
        agent's own path and query are mirrored; nothing else is invented."""
        path = str(frame.get("path", ""))
        query = str(frame.get("query", ""))
        return "ws" + self.upstream[len("http"):] + path + (("?" + query) if query else "")

    async def _send(self, frame: dict) -> None:
        if self.ws is None or self.ws.closed:
            return
        try:
            await self.ws.send_json(frame)
        except (aiohttp.ClientError, OSError, RuntimeError):
            pass

    async def _report(self, *, connected: bool, error: str = "") -> None:
        await asyncio.to_thread(self._status, self.target.id,
                                connected=connected, error=error)

    def _keep(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class LinkManager:
    """One uplink per enabled remote relay, reloaded from the database so an
    admin's edits take effect without restarting the process."""

    def __init__(self, *, loader=load_targets, uplink_factory=Uplink):
        self._loader = loader
        self._uplink_factory = uplink_factory
        self.runtasks: dict[str, asyncio.Task] = {}
        self.targets: dict[str, Target] = {}

    async def run(self) -> None:
        while True:
            try:
                targets = await asyncio.to_thread(self._loader)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError) as exc:
                # A reload that fails is not a reason to stop linking. It is also
                # not a reason to disconnect everything: an empty target list here
                # would retire every uplink over a transient database error, so we
                # keep whatever was last known good and try again next pass.
                log.warning("relay link could not load relays: %s", _describe(exc))
                await asyncio.sleep(RELOAD_SECONDS)
                continue
            self._reconcile(targets)
            await asyncio.sleep(RELOAD_SECONDS)

    def _reconcile(self, targets: list[Target]) -> None:
        current = {target.id: target for target in targets}
        for rid, task in list(self.runtasks.items()):
            if rid not in current or _changed(self.targets.get(rid), current[rid]):
                task.cancel()
                del self.runtasks[rid]
                self.targets.pop(rid, None)
        for rid, target in current.items():
            if rid not in self.runtasks:
                uplink = self._uplink_factory(target)
                self.runtasks[rid] = asyncio.create_task(uplink.run())
                self.targets[rid] = target

    async def stop(self) -> None:
        for task in list(self.runtasks.values()):
            task.cancel()
        for task in list(self.runtasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.runtasks.clear()
        self.targets.clear()


def _changed(old: Target | None, new: Target) -> bool:
    return old is None or old.key_sha256 != new.key_sha256 or old.url != new.url


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _reason(exc: BaseException) -> str:
    if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status == 401:
        return "relay rejected the key"
    return _describe(exc)


def _b64decode(value) -> bytes:
    if not value:
        return b""
    try:
        return base64.b64decode(value, validate=False)
    except (ValueError, TypeError):
        return b""


def _b64encode(raw: bytes) -> str:
    if isinstance(raw, str):
        raw = raw.encode()
    return base64.b64encode(raw).decode()


def _text(value) -> str:
    """A TEXT payload's text, whether the client gave us str or (with decoding
    off) utf-8 bytes."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
