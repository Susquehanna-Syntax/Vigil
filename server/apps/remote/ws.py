"""WebSockets: the browser side, the agent side, and nothing in between them
but a pair of queues this process supervises.

Neither peer decides anything about state. The browser cannot claim a session
is open; the agent cannot keep one running after its user signed out. Every
frame is JSON text and every payload byte is base64, so an intermediate proxy
never has to guess where a message ends.
"""

import asyncio
import base64
import binascii
import hmac
import importlib
import json
import logging
import re
import time
import types
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user
from django.http.request import validate_host
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.relays.models import direct_allowed
from apps.relays.trust import link_token

from . import bus, rdp
from .models import RemoteSession
from .recording import CastWriter, recording_allowed

logger = logging.getLogger(__name__)

#: A frame's payload larger than this is dropped, not forwarded: a peer that
#: starts spamming must not be able to bury the other end.
MAX_FRAME_BYTES = 64 * 1024

IDLE_SECONDS = 15 * 60
HARD_LIMIT_SECONDS = 8 * 60 * 60
#: How long to wait for the agent to answer before giving up on the session.
AGENT_ANSWER_SECONDS = 45
SIGNIN_CHECK_SECONDS = 60
WATCHDOG_TICK = 5.0

BROWSER_ROUTE = re.compile(r"^/ws/session/(?P<sid>[0-9a-f-]{36})/$")
AGENT_ROUTE = re.compile(r"^/ws/agent/session/(?P<sid>[0-9a-f-]{36})/$")


class Closed(Exception):
    """Refuse the socket with this code. Raised before ``websocket.accept``,
    when there is nothing else to say."""

    def __init__(self, code: int):
        super().__init__(f"close {code}")
        self.code = code


class Ending(Exception):
    """One task's turn to end the session, with the reason it goes in the log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def websocket_app(scope, receive, send):
    path = scope.get("path", "")
    for pattern, handler in ((BROWSER_ROUTE, browser_socket), (AGENT_ROUTE, agent_socket)):
        match = pattern.match(path)
        if match:
            return await handler(scope, receive, send, match.group("sid"))

    message = await receive()
    if message["type"] == "websocket.connect":
        await _close_quietly(send, 4404)


# ---------------------------------------------------------------------------
# Browser side
# ---------------------------------------------------------------------------

async def browser_socket(scope, receive, send, sid):
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    try:
        user, session = await _authorize_browser(scope, receive, sid)
    except Closed as exc:
        await _close_quietly(send, exc.code)
        return

    if session.kind == RemoteSession.Kind.RDP:
        # A desktop is guacd's protocol, not this file's terminal loop. Every
        # check above still decided who is here; run_browser accepts (with the
        # guacamole subprotocol) and gets this file's watchdog and open marker.
        state = _SessionState(cookie=_cookie_from_scope(scope))

        async def opened() -> None:
            state.opened = True
            await sync_to_async(_mark_open, thread_sensitive=True)(session)

        def touched() -> None:
            state.last_input = time.monotonic()

        reason = await rdp.run_browser(
            scope, receive, send, session,
            accept=_accept_for(send), close=_close_for(send),
            watchdog=lambda: _watchdog(session, user, state, _Limits()),
            on_open=opened, on_input=touched)
    else:
        await send({"type": "websocket.accept"})
        await _send_json(send, {"t": "status", "state": "waiting"})

        state = _SessionState(cookie=_cookie_from_scope(scope))
        recording = await sync_to_async(_open_recording, thread_sensitive=True)(session)
        reason = "closed"
        try:
            await _run_browser(receive, send, session, user, state, recording)
        except Ending as end:
            reason = end.reason
        finally:
            if recording is not None:
                await recording.aclose()

    await bus.push(bus.B2A_KEY.format(sid=str(session.id)),
                   {"t": "close", "reason": reason})
    expired = reason == "the agent did not answer"
    await sync_to_async(_finish, thread_sensitive=True)(
        session, "expired" if expired else "closed", reason)
    await sync_to_async(_audit_end, thread_sensitive=True)(session, reason)
    # A desktop socket speaks Guacamole, and a JSON frame on it would break the
    # browser's parser: the close code and the session row carry the reason.
    if session.kind != RemoteSession.Kind.RDP:
        try:
            await _send_json(send, {"t": "status", "state": "closed", "reason": reason})
        except RuntimeError:
            # The browser went away first; the row and the audit are what matter.
            logger.debug("browser already gone for session %s", sid)
    await _close_quietly(send, 1000)


class _SessionState:
    """What the watchdog reads: when the human last typed, and whether they are
    still who they were when the socket opened."""

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.started = time.monotonic()
        self.last_input = time.monotonic()
        self.opened = False


async def _run_browser(receive, send, session, user, state, recording):
    """Pump both directions and the watchdog until one of them says enough.

    The tasks never raise in normal operation — each *returns* its reason and
    stops, which is what makes this safe to supervise: an exception reaching
    here is a bug, and the remaining tasks are cancelled either way so no
    session outlives the task that ended it.
    """
    tasks = [
        asyncio.create_task(_browser_reader(receive, session, state)),
        asyncio.create_task(_browser_writer(send, session, state, recording)),
        asyncio.create_task(_watchdog(session, user, state, _Limits())),
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            reason = task.result()
            if isinstance(reason, str):
                raise Ending(reason)
        # Only the watchdog ending without a reason could get here; it cannot.
        raise Ending("closed")
    finally:
        for other in tasks:
            other.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _accept_for(send):
    """The accept, with the subprotocol echoed only when the client offered it:
    a browser that never asked for ``guacamole`` must not be handed a protocol
    it did not negotiate."""

    async def accept(subprotocol) -> None:
        event = {"type": "websocket.accept"}
        if subprotocol:
            event["subprotocol"] = subprotocol
        await send(event)

    return accept


def _close_for(send):
    async def close(code: int) -> None:
        await _close_quietly(send, code)

    return close


async def _browser_reader(receive, session, state):
    b2a = bus.B2A_KEY.format(sid=str(session.id))
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return "browser closed"
        if kind != "websocket.receive":
            continue
        frame = _decode_frame(message.get("text"))
        if frame is None:
            continue
        t = frame.get("t")
        if t == "in":
            data = _decode_b64(frame.get("d"))
            if data is None or len(data) > MAX_FRAME_BYTES:
                continue
            state.last_input = time.monotonic()
            await bus.push(b2a, {"t": "in", "d": frame["d"]})
            # Counted in memory; _finish writes the totals once.
            session.bytes_in += len(data)
        elif t == "resize":
            cols, rows = frame.get("cols"), frame.get("rows")
            if (isinstance(cols, int) and isinstance(rows, int)
                    and 1 <= cols <= 500 and 1 <= rows <= 300):
                await bus.push(b2a, {"t": "resize", "cols": cols, "rows": rows})


async def _browser_writer(send, session, state, recording):
    a2b = bus.A2B_KEY.format(sid=str(session.id))
    while True:
        item = await bus.pop(a2b, 1.0)
        if item is None:
            continue
        t = item.get("t")
        if t == "out":
            data = _decode_b64(item.get("d"))
            if data is None:
                continue
            await _send_json(send, {"t": "out", "d": item["d"]})
            session.bytes_out += len(data)
            if recording is not None:
                await recording.write_output(data)
        elif t == "open":
            state.opened = True
            await sync_to_async(_mark_open, thread_sensitive=True)(session)
            await _send_json(send, {"t": "status", "state": "open"})
        elif t == "mode":
            await _send_json(send, {"t": "mode", "pty": bool(item.get("pty"))})
        elif t == "resize" and recording is not None:
            await recording.resize(item.get("cols"), item.get("rows"))
        elif t == "close":
            return str(item.get("reason") or "closed by operator")[:120]


class _Limits:
    """The watchdog's thresholds, read from the module when a session starts so
    a test can patch them before connecting."""

    def __init__(self) -> None:
        self.idle = IDLE_SECONDS
        self.hard = HARD_LIMIT_SECONDS
        self.agent_answer = AGENT_ANSWER_SECONDS
        self.tick = WATCHDOG_TICK
        #: How often to ask whether the login behind this socket is still valid.
        self.signin_check = SIGNIN_CHECK_SECONDS


async def _watchdog(session, user, state, limits):
    """The only clock a session trusts: its own deadlines, and its user."""
    created = session.created_at.timestamp() if session.created_at else time.time()
    checked_at = 0.0
    while True:
        await asyncio.sleep(limits.tick)
        elapsed = time.monotonic() - state.started
        if elapsed > limits.hard:
            return "time limit"
        if time.monotonic() - state.last_input > limits.idle:
            return "idle timeout"
        if not state.opened and (time.time() - created) > limits.agent_answer:
            return "the agent did not answer"
        if elapsed - checked_at >= limits.signin_check:
            checked_at = elapsed
            if not await _still_signed_in(user, state.cookie):
                return "signed out"


# ---------------------------------------------------------------------------
# Agent side
# ---------------------------------------------------------------------------

async def agent_socket(scope, receive, send, sid):
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    try:
        await _authorize_agent(_headers(scope), sid)
    except Closed as exc:
        await _close_quietly(send, exc.code)
        return

    await send({"type": "websocket.accept"})
    # Only the server decides a session is open; the agent is told, never asked.
    await bus.push(bus.A2B_KEY.format(sid=sid), {"t": "open"})

    tasks = [
        asyncio.create_task(_agent_reader(receive, bus.A2B_KEY.format(sid=sid))),
        asyncio.create_task(_agent_writer(send, bus.B2A_KEY.format(sid=sid))),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Whichever side ended it, the browser hears. A close it already sent is
    # harmless: its writer has stopped reading.
    await bus.push(bus.A2B_KEY.format(sid=sid),
                   {"t": "close", "reason": "the agent disconnected"})
    await _close_quietly(send, 1000)


async def _agent_reader(receive, a2b):
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return
        if kind != "websocket.receive":
            continue
        frame = _decode_frame(message.get("text"))
        if frame is None:
            continue
        if frame.get("t") == "mode" and isinstance(frame.get("pty"), bool):
            # Whether the agent has a terminal: without one (Windows pipes) the
            # browser echoes locally. Rebuilt, so nothing else rides along.
            await bus.push(a2b, {"t": "mode", "pty": frame["pty"]})
            continue
        if frame.get("t") != "out":
            continue
        data = _decode_b64(frame.get("d"))
        if data is None or len(data) > MAX_FRAME_BYTES:
            continue
        await bus.push(a2b, {"t": "out", "d": frame["d"]})


async def _agent_writer(send, b2a):
    while True:
        item = await bus.pop(b2a, 1.0)
        if item is None:
            continue
        t = item.get("t")
        if t not in ("in", "resize", "close"):
            continue
        await _send_json(send, item)
        if t == "close":
            return


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

async def _authorize_browser(scope, receive, sid):
    from django.contrib.auth.models import AnonymousUser

    if not _origin_allowed(scope):
        raise Closed(4403)
    cookie = _cookie_from_scope(scope)
    user = await sync_to_async(_user_from_cookie, thread_sensitive=True)(cookie)
    if isinstance(user, AnonymousUser):
        raise Closed(4401)

    session = await sync_to_async(_load_session, thread_sensitive=True)(sid)
    if session is None or session.user_id != user.id:
        raise Closed(4404)
    if session.state not in (RemoteSession.State.PENDING, RemoteSession.State.OPEN):
        raise Closed(4404)

    if not await bus.claim_browser(bus.BROWSER_KEY.format(sid=sid)):
        raise Closed(4409)
    return user, session


async def _authorize_agent(headers, sid):
    relayed = hmac.compare_digest(headers.get(b"x-vigil-link", b"").decode("latin-1"),
                                  link_token())
    # The same gate every other agent path passes: through the link, or direct
    # only while the built-in relay is enabled. Before any lookup, so a refused
    # caller learns nothing about which sessions exist.
    if not relayed and not await sync_to_async(direct_allowed, thread_sensitive=True)():
        raise Closed(4403)

    session = await sync_to_async(_load_session, thread_sensitive=True)(sid)
    if session is None:
        raise Closed(4404)

    bearer = headers.get(b"authorization", b"").decode("latin-1")
    if not bearer.startswith("Bearer "):
        raise Closed(4401)
    host = await sync_to_async(_host_by_token, thread_sensitive=True)(bearer[7:].strip())
    # The token must belong to *this* session's host — otherwise any enrolled
    # agent could be handed someone else's shell.
    if host is None or host.id != session.host_id:
        raise Closed(4401)
    if session.state != RemoteSession.State.PENDING:
        raise Closed(4404)
    # One agent socket per session: a second, even with the right token, would
    # get a copy of the browser's keystrokes.
    if not await bus.claim_browser(bus.AGENT_KEY.format(sid=sid)):
        raise Closed(4409)


def _load_session(sid):
    # With its host: rdp.desktop_protocol reads the OS from an async context.
    return RemoteSession.objects.select_related("host").filter(pk=sid).first()


def _host_by_token(token: str):
    try:
        return Host.objects.by_token(token).get()
    except Host.DoesNotExist:
        return None


def _user_from_cookie(cookie: str):
    engine = importlib.import_module(settings.SESSION_ENGINE)
    store = engine.SessionStore(cookie)
    # get_user re-checks the session auth hash, so a password change or logout
    # invalidates the login this socket was opened with.
    return get_user(types.SimpleNamespace(session=store))


async def _still_signed_in(user, cookie: str) -> bool:
    from django.contrib.auth.models import AnonymousUser

    resolved = await sync_to_async(_user_from_cookie, thread_sensitive=True)(cookie)
    return not isinstance(resolved, AnonymousUser) and resolved.id == user.id


def _origin_allowed(scope) -> bool:
    """Cross-site WebSocket hijacking needs no CSRF token: a browser sends the
    session cookie with a ``ws://`` upgrade whatever the page's origin is. So
    Origin is checked here rather than left to Django's CSRF machinery."""
    headers = _headers(scope)
    origin = headers.get(b"origin", b"").decode("latin-1").rstrip("/")
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if origin in settings.CSRF_TRUSTED_ORIGINS:
        return True
    # Otherwise the page must be this site itself: the Origin names the same
    # host (and port) the upgrade was sent to, and that host is one Vigil
    # answers to. A host merely listed in ALLOWED_HOSTS is not enough: with a
    # wildcard there, any site would pass.
    host = headers.get(b"host", b"").decode("latin-1").lower()
    return bool(host) and parts.netloc.lower() == host and validate_host(
        host.rsplit(":", 1)[0] if not host.endswith("]") else host,
        settings.ALLOWED_HOSTS)


def _headers(scope) -> dict[bytes, bytes]:
    return {name: value for name, value in scope.get("headers") or []}


def _cookie_from_scope(scope) -> str:
    wanted = settings.SESSION_COOKIE_NAME.encode()
    raw = _headers(scope).get(b"cookie", b"").decode("latin-1")
    for piece in raw.split(";"):
        name, _, value = piece.strip().partition("=")
        if name.encode() == wanted:
            return value
    return ""


# ---------------------------------------------------------------------------
# Rows, audits, frames
# ---------------------------------------------------------------------------

def _mark_open(session) -> None:
    if session.state == RemoteSession.State.PENDING:
        session.state = RemoteSession.State.OPEN
        session.opened_at = now()
        RemoteSession.objects.filter(pk=session.pk).update(
            state=session.state, opened_at=session.opened_at)


def _finish(session, new_state: str, reason: str) -> None:
    session.state = new_state
    session.close_reason = reason[:120]
    session.closed_at = now()
    RemoteSession.objects.filter(pk=session.pk).update(
        state=new_state, closed_at=session.closed_at,
        close_reason=session.close_reason, bytes_in=session.bytes_in,
        bytes_out=session.bytes_out)


def _open_recording(session):
    if not recording_allowed(session.kind):
        return None
    try:
        writer = CastWriter.for_session(session)
    except OSError as exc:
        # A recording we cannot write is an audit failure, not a reason to drop
        # the session — say so loudly and carry on without it.
        logger.warning("cannot record session %s: %s", session.pk, exc)
        return None
    session.recording = writer.relative_path
    RemoteSession.objects.filter(pk=session.pk).update(recording=writer.relative_path)
    return writer


def _audit_end(session, reason: str) -> None:
    """Audit a session end. Core must not hard-depend on apps_business."""
    try:
        from apps_business.audits.models import record
    except ImportError:
        return
    seconds = None
    if session.opened_at and session.closed_at:
        seconds = (session.closed_at - session.opened_at).total_seconds()
    record("remote.session_ended", user=session.user, target=session.host.hostname,
           session=str(session.id), kind=session.kind, reason=reason,
           duration_seconds=seconds, bytes_in=session.bytes_in,
           bytes_out=session.bytes_out)


def _decode_frame(text):
    if not isinstance(text, str):
        return None
    try:
        frame = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return frame if isinstance(frame, dict) else None


def _decode_b64(value):
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value.encode(), validate=True)
    except (binascii.Error, ValueError):
        return None


async def _send_json(send, payload: dict) -> None:
    await send({"type": "websocket.send", "text": json.dumps(payload)})


async def _close_quietly(send, code: int) -> None:
    """A close that fails means the peer is already gone — which is the outcome
    the caller wanted anyway."""
    try:
        await send({"type": "websocket.close", "code": code})
    except Exception:  # noqa: BLE001 — nothing left to negotiate with.
        logger.debug("socket already closed while sending %s", code)
