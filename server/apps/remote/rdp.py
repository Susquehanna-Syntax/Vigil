"""Web RDP: Vigil as the middle of a Guacamole connection.

guacd speaks RDP; neither the browser nor the agent does. Vigil sits between them
and forwards bytes in both directions without interpreting them:

``browser ──ws(guac text)── web ──tcp── guacd ──tcp(RDP)── bridge ══bus══ agent ── 127.0.0.1:3389``

Two things here are Vigil's own protocol: the one JSON message with which the
browser hands over its Windows credentials, and the one-shot TCP server guacd
connects back to in order to reach the target host through the session bus. The
credentials exist only long enough to write the ``connect`` instruction — nothing
here logs them, puts them on the bus, or holds them after the handshake. A
desktop's pixels are not a terminal stream either, so recording is guacd's job
(Business tier), not CastWriter's.

The browser half arrives through ``ws.browser_socket``, which keeps every check
it already made; ``run_browser`` is what those checks unlock.
"""

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import socket
from codecs import getincrementaldecoder

from django.conf import settings

from vigil.licensing import has_feature

from . import bus

logger = logging.getLogger(__name__)

#: The subprotocol the browser offers and guacd answers by.
GUACAMOLE_SUBPROTOCOL = "guacamole"

HANDSHAKE_SECONDS = 10.0
LOGIN_SECONDS = 60.0
BRIDGE_TIMEOUT_SECONDS = 30.0
GUACD_CONNECT_SECONDS = 5.0

#: guacd's RDP bytes go on the bus in reads this size, like the agent's.
BRIDGE_READ_BYTES = 16 * 1024
MAX_WIDTH, MIN_WIDTH, DEFAULT_WIDTH = 3840, 320, 1024
MAX_HEIGHT, MIN_HEIGHT, DEFAULT_HEIGHT = 2160, 240, 768
MAX_DPI, MIN_DPI, DEFAULT_DPI = 288, 72, 96

#: The three parameters only a recording install asks for. An empty value is
#: guacd's "not asked", which is what makes conditional parameters work at all.
RECORDING_PARAMS = frozenset({"recording-path", "recording-name",
                              "create-recording-path"})


class RdpError(Exception):
    """This desktop cannot be opened. ``code`` is the close code for the browser,
    when there is a browser left to hand it to."""

    def __init__(self, message: str, code: int = 4503):
        super().__init__(message)
        self.code = code


class _BrowserGone(RdpError):
    """The browser hung up during the login wait. There is no socket left to
    send a close code to, and nothing here is worth a log line."""

    def __init__(self) -> None:
        super().__init__("browser closed", 1000)


# ---------------------------------------------------------------------------
# The Guacamole instruction codec
# ---------------------------------------------------------------------------

def encode(*elements) -> str:
    """One instruction: ``<length>.<value>`` elements, comma-separated, then ``;``.

    Lengths count Unicode code points rather than bytes; guacd counts them the
    same way, so a window title in Chinese is one character per square.
    """
    return ",".join(f"{len(str(el))}.{el}" for el in elements) + ";"


def _wire(*elements) -> bytes:
    """An instruction as the socket wants it. Text carries instructions between
    Vigil's halves; only guacd's socket speaks bytes."""
    return encode(*elements).encode("utf-8")


class Decoder:
    """The other half: text in, complete instructions out.

    One instruction arrives split across three WebSocket frames as often as it
    arrives whole, so a partial element is kept until the rest of it turns up.
    The element list is an instance attribute rather than a shared one: two
    sessions parse at once, and a desktop's instructions must not mix.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._element: list[str] = []

    def feed(self, text: str) -> list[list[str]]:
        self._buffer += text
        instructions: list[list[str]] = []
        while True:
            # An element is "<length>.<value>", and the length counts its value's
            # code points. So the length is read up to the first ".", and it says
            # exactly where the value ends — whatever byte lands there is the
            # separator, "," inside an instruction or ";" at its end.
            dot = self._buffer.find(".")
            if dot < 0:
                return instructions
            if not self._buffer[:dot].isdigit():
                raise RdpError("malformed guacamole element length")
            edge = dot + 1 + int(self._buffer[:dot])
            if len(self._buffer) <= edge:
                # Not all of the value has arrived. A comma inside it may still be
                # data rather than a separator, and guessing is how a desktop's
                # drawing ends up misframed, so the rest is simply waited for.
                return instructions
            value = self._buffer[dot + 1:edge]
            terminator = self._buffer[edge]
            if terminator not in (",", ";"):
                raise RdpError("malformed guacamole instruction")
            self._buffer = self._buffer[edge + 1:]
            self._element.append(value)
            if terminator == ";":
                instructions.append(self._element)
                self._element = []


class InstructionText:
    """Bytes → instructions, tolerant of a multibyte character split across reads."""

    def __init__(self) -> None:
        self._decoder = getincrementaldecoder("utf-8")()
        self._parser = Decoder()

    def feed(self, data: bytes) -> list[list[str]]:
        return self._parser.feed(self._decoder.decode(data))


# ---------------------------------------------------------------------------
# Handshake with guacd
# ---------------------------------------------------------------------------

async def handshake(reader, writer, params: dict, *, recording: bool) -> None:
    """Select rdp, describe this client, then connect.

    ``params`` carries the values the phase doc's table names. A parameter guacd
    asks for that ``params`` does not hold goes over empty, which is guacd's own
    "not specified" — that is how one handshake serves every guacd build.
    """
    args = await _exchange(reader, writer, _wire("select", "rdp"))
    if args[0] != "args":
        raise RdpError("guacd did not answer the protocol selection")

    # guacd answers select once, with args: the protocol version first, then the
    # parameter names. The client then describes itself (no reply comes to
    # that) and sends connect with one value per element of args, in order:
    # the version echoed back, then each parameter's value.
    names = args[2:]
    values = [_echo_version(args, params)]
    values += [_param(name, params, recording) for name in names]
    ready = await _exchange(reader, writer,
                            _client_info(params) + _wire("connect", *values))
    if ready[0] != "ready":
        raise RdpError("guacd did not accept the connection")


def _client_info(params: dict) -> bytes:
    """What this client can do. guacd sizes its buffers from these, so they come
    before ``connect``; one write keeps their order fixed on the wire."""
    width = _clamped(params.get("width"), MIN_WIDTH, MAX_WIDTH, DEFAULT_WIDTH)
    height = _clamped(params.get("height"), MIN_HEIGHT, MAX_HEIGHT, DEFAULT_HEIGHT)
    dpi = _clamped(params.get("dpi"), MIN_DPI, MAX_DPI, DEFAULT_DPI)
    return (encode("size", width, height, dpi)
            + encode("audio")
            + encode("video")
            + encode("image", "image/png", "image/jpeg")
            + encode("timezone", "UTC")).encode("utf-8")


def _echo_version(select: list[str], params: dict) -> str:
    """The first ``connect`` value is the protocol version guacd named for
    itself, echoed back in its own position: that is how guacd lines its
    parameter names up with our values.

    It is also the only version-sensitive thing we send. ``security`` grew its
    NLA spellings — "any" among them — in protocol 1.4.0, so against an older
    guacd this goes over as the value that one understands.
    """
    echoed = select[1] if len(select) > 1 else ""
    parts = echoed.removeprefix("VERSION_").split("_") if echoed.startswith("VERSION_") else []
    try:
        minor = int(parts[1])
    except (IndexError, ValueError):
        return echoed
    if minor < 4 and str(params.get("security", "")).lower() == "any":
        params["security"] = "rdp"
    return echoed


async def _exchange(reader, writer, instruction: bytes) -> list[str]:
    """Write *instruction* (when there is one) and read the instruction back.

    An empty *instruction* means "read what is already on its way", which is how
    guacd's ``args`` after ``select`` is collected: it sends that unprompted.
    """
    if instruction:
        writer.write(instruction)
        await writer.drain()
    parts = await _read_instruction(reader)
    if parts[0] == "error":
        raise RdpError(f"guacd refused the session: {_guac_message(parts)}")
    return parts


async def _read_instruction(reader, timeout: float | None = None) -> list[str]:
    """One instruction from guacd's handshake. Lengths are code points, so the
    bytes are decoded as text rather than assumed to be ASCII."""
    if timeout is None:
        timeout = HANDSHAKE_SECONDS   # read now, so a test can shorten it
    text = InstructionText()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RdpError("guacd did not answer the handshake")
        try:
            chunk = await asyncio.wait_for(reader.read(4096), remaining)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise RdpError("guacd did not answer the handshake in time") from exc
        if not chunk:
            raise RdpError("guacd closed the connection")
        for instruction in text.feed(chunk):
            return instruction


def _guac_message(parts: list[str]) -> str:
    """The message of an ``error`` instruction, printable and short: it is the
    one guacd sentence a browser gets to see."""
    text = parts[1] if len(parts) > 1 else "rejected"
    return "".join(ch for ch in text if ch.isprintable())[:120]


def _param(name: str, params: dict, recording: bool) -> str:
    if name in RECORDING_PARAMS and not recording:
        return ""
    return str(params.get(name, "") or "")


def _clamped(value, low: int, high: int, default: int) -> str:
    """A browser's own screen size, inside the range guacd will accept. A value
    outside it is a bug in the page or an attack; both get a usable desktop."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return str(min(max(number, low), high))


# ---------------------------------------------------------------------------
# The one-shot bridge: guacd's way back to the agent
# ---------------------------------------------------------------------------

class OneShotBridge:
    """A TCP server that exists exactly once.

    guacd is told to connect here instead of at the target host, so the RDP bytes
    arrive in this process and go out on the session bus like any other byte. It
    listens on all interfaces because guacd reaches this container by name — so
    it accepts a connection from one peer only, an address ``VIGIL_GUACD_ADDR``'s
    host resolved to when the bridge started, exactly once, and only for as long
    as a handshake takes. After that the port belongs to whoever is scanning.
    """

    def __init__(self, sid, guacd_host: str = "",
                 timeout: float | None = None) -> None:
        self.sid = str(sid)
        self._guacd_host = guacd_host
        self.timeout = BRIDGE_TIMEOUT_SECONDS if timeout is None else timeout
        self.port: int | None = None
        #: Connections turned away, for the log line. Never their payload.
        self.refused = 0
        self.peer: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self._allowed: list = []
        self._writer = None
        #: Awaited when the agent's socket opens (its {"t":"open"} on a2b).
        self.on_open = None

    async def start(self) -> int:
        """Open the port and set the one deadline that closes it again.

        The resolve happens here rather than per connection: guacd's address is
        decided once, and an attacker cannot win a DNS race against a check that
        has already been made.
        """
        self._allowed = await asyncio.to_thread(_resolve, self._guacd_host)
        self._server = await asyncio.start_server(self._handle, host="0.0.0.0", port=0)
        self.port = self._server.sockets[0].getsockname()[1]
        asyncio.get_running_loop().call_later(self.timeout, self.close)
        return self.port

    async def _handle(self, reader, writer) -> None:
        peer = _peer_of(writer)
        if not self._allows(peer):
            # Refused before a single byte is read: a probe learns nothing about
            # whether the port is Vigil's, or what is behind it.
            self.refused += 1
            logger.warning("rdp bridge refused a connection from %s", peer)
            writer.close()
            await _quietly(writer.wait_closed())
            return
        if self._writer is not None:
            # The desktop has its path. A second peer on it would be a way to
            # watch, or to inject, someone else's session.
            logger.warning("rdp bridge refused a second connection from %s", peer)
            writer.close()
            await _quietly(writer.wait_closed())
            return
        self.peer = peer
        self._writer = writer
        b2a = bus.B2A_KEY.format(sid=self.sid)
        try:
            while True:
                data = await reader.read(BRIDGE_READ_BYTES)
                if not data:
                    return
                await bus.push(b2a, {"t": "in", "d": base64.b64encode(data).decode()})
        except (ConnectionError, OSError):
            pass
        finally:
            self._writer = None
            writer.close()

    def _allows(self, peer: str) -> bool:
        if not self._allowed:
            # Nothing guacd's name resolved to, so nothing may connect.
            return False
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return address in self._allowed

    async def pipe(self, sid=None) -> None:
        """Between the bus and guacd's connection, in both directions.

        Socket bytes go out as ``in`` on the browser-to-agent key; what the agent
        sends comes back as ``out`` on the other. A close on either side ends it,
        so a desktop dies with its session rather than outliving the socket that
        opened it. Stale queue items are skipped: they would be replayed into a
        live RDP stream, where a byte at the wrong moment is protocol corruption.
        """
        target = str(sid or self.sid)
        a2b = bus.A2B_KEY.format(sid=target)
        while True:
            item = await bus.pop(a2b, 1.0)
            if item is None:
                continue
            kind = item.get("t")
            if kind == "close":
                return str(item.get("reason") or "closed")[:120]
            if kind == "open":
                if self.on_open is not None:
                    await self.on_open()
                continue
            if kind != "out":
                continue
            data = _decode_b64(item.get("d"))
            writer = self._writer
            if not data or writer is None:
                continue
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionError, OSError):
                return

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()


def _peer_of(writer) -> str:
    try:
        return str(writer.get_extra_info("peername")[0])
    except (TypeError, IndexError):
        return ""


async def _quietly(coro) -> None:
    """A close that fails means the peer is already gone — the outcome wanted."""
    try:
        await coro
    except (ConnectionError, OSError):
        pass


def _resolve(host: str) -> list:
    """Every address guacd's host name resolves to, as ``ipaddress`` objects.

    An empty or unresolvable name yields nothing, and a bridge that allows no
    address accepts no connection: failing closed is the point of it.
    """
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning("rdp bridge cannot resolve %s", host)
        return []
    found: list = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    return found


def _decode_b64(value):
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value.encode(), validate=True)
    except (binascii.Error, ValueError):
        return None


# ---------------------------------------------------------------------------
# The browser side of a desktop session
# ---------------------------------------------------------------------------

async def run_browser(scope, receive, send, session, *, accept, close,
                      watchdog=None, on_open=None, on_input=None):
    """The desktop half of a browser socket: credentials, guacd, then bytes.

    ``accept``/``close`` are ``ws.py``'s own, so the way a socket is accepted or
    refused lives in one file. The string returned is the reason ``ws.py`` writes
    to the session row, which it does exactly as for a shell.
    """
    offered = scope.get("subprotocols") or []
    await accept(GUACAMOLE_SUBPROTOCOL if GUACAMOLE_SUBPROTOCOL in offered else None)

    sid = str(session.id)
    try:
        params = await _login(receive, close)
    except RdpError as exc:
        return str(exc) or "no credentials"

    bridge = OneShotBridge(sid, _guacd_host())
    bridge.on_open = on_open
    guacd_reader = guacd_writer = None
    tasks: list[asyncio.Task] = []
    try:
        if not _guacd_addr():
            raise RdpError("Web RDP is not configured on this server")
        port = await bridge.start()
        recording = await _recording(session)
        guacd_reader, guacd_writer = await _connect_guacd(_guacd_addr())
        try:
            await handshake(guacd_reader, guacd_writer,
                            _handshake_params(params, session, str(port), recording),
                            recording=bool(recording))
        finally:
            # The credentials have done their one job. Holding them past the
            # connect instruction is how they reach a log, or a traceback.
            del params["password"], params["username"]
        if recording:
            await _save_recording(session, recording)
        tasks = [asyncio.create_task(bridge.pipe()),
                 asyncio.create_task(_guacd_to_browser(guacd_reader, send)),
                 asyncio.create_task(_browser_to_guacd(receive, guacd_writer, on_input))]
        if watchdog is not None:
            # The same limits as a shell: idle, the 8-hour cap, an agent that
            # never answered, and the login behind the socket ending.
            tasks.append(asyncio.create_task(watchdog()))
        return await _supervise(tasks)
    except RdpError as exc:
        if not isinstance(exc, _BrowserGone):
            await close(exc.code)
        return str(exc) or "desktop refused"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bridge.close()
        if guacd_writer is not None:
            guacd_writer.close()


async def _login(receive, close, timeout: float | None = None) -> dict:
    """The browser's one credential message.

    Nothing else may arrive first and nothing here is logged: this dict is the
    password's only home, and it is a local one. The wait is a *connect* timeout
    rather than a read one — an RDP stream is bidirectional from its first
    instruction, so a frame the operator sends next must not be lost because the
    deadline passed while it was in flight.
    """
    if timeout is None:
        timeout = LOGIN_SECONDS   # read now, so a test can shorten it
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            message = await _next_message(receive, remaining)
        except (asyncio.TimeoutError, TimeoutError):
            break
        kind = message.get("type")
        if kind == "websocket.disconnect":
            raise _BrowserGone()
        if kind != "websocket.receive":
            continue
        frame = _json_object(message.get("text"))
        if frame is None or frame.get("t") != "rdp_login":
            break
        username, password = frame.get("username"), frame.get("password")
        # An empty Windows password is an ordinary account; a missing username
        # or a non-string password is a page that did not send the form.
        if not isinstance(username, str) or not username:
            break
        if not isinstance(password, str):
            break
        return {
            "username": username,
            "password": password,
            "domain": str(frame.get("domain") or ""),
            "width": frame.get("width"),
            "height": frame.get("height"),
            "dpi": frame.get("dpi"),
        }
    await close(4400)
    raise RdpError("no credentials", 4400)


async def _next_message(receive, timeout: float) -> dict:
    """The browser's next message, with a deadline that cannot swallow it.

    ``wait_for`` cancels the coroutine it awaits, so a reader already holding a
    dequeued frame would drop it when the deadline passed. Running the read as a
    shielded task leaves that message where the next stage can find it; only the
    wait is cut short. The task is left running on purpose — cancelling it is
    exactly what loses the frame.
    """
    return await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(receive())),
                                  timeout)


def _handshake_params(params: dict, session, port: str, recording: str) -> dict:
    """The browser's message plus everything guacd takes that Vigil decides. The
    hostname is the *bridge*, never the target: guacd must come back through us."""
    out = {
        "hostname": str(getattr(settings, "VIGIL_RDP_BRIDGE_HOST", "web") or "web"),
        "port": port,
        "username": params.get("username", ""),
        "password": params.get("password", ""),
        "domain": params.get("domain", ""),
        "security": "any",
        "ignore-cert": "true",
        "width": params.get("width"),
        "height": params.get("height"),
        "resize-method": "display-update",
    }
    if recording:
        out["recording-path"] = str(getattr(settings, "VIGIL_RDP_RECORDING_PATH",
                                            "/recordings"))
        # The file name guacd writes: the last part of the stored path.
        out["recording-name"] = recording.rsplit("/", 1)[-1]
        out["create-recording-path"] = "true"
    return out


def _json_object(text):
    if not isinstance(text, str):
        return None
    try:
        frame = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return frame if isinstance(frame, dict) else None


async def _connect_guacd(addr: str):
    host, _, port = addr.rpartition(":")
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(host or addr, int(port or 4822)),
            GUACD_CONNECT_SECONDS)
    except (OSError, ValueError, asyncio.TimeoutError, TimeoutError) as exc:
        raise RdpError("cannot reach the guacd service") from exc


def _guacd_addr() -> str:
    return str(getattr(settings, "VIGIL_GUACD_ADDR", "") or "")


def _guacd_host() -> str:
    """The name to resolve for the bridge's peer check — never its port."""
    return _guacd_addr().rpartition(":")[0]


async def _guacd_to_browser(reader, send) -> str:
    """Raw Guacamole text out of guacd and into the browser as text frames.

    Decoded incrementally because a multibyte character — a window title, a
    filename on the desktop — may be split across two TCP reads, and a frame
    boundary is not a character boundary. The instructions are re-encoded rather
    than forwarded whole, so what the browser's parser sees is well-formed even
    if guacd packed several into one read.
    """
    text = InstructionText()
    while True:
        data = await reader.read(64 * 1024)
        if not data:
            return "guacd closed"
        for instruction in text.feed(data):
            await send({"type": "websocket.send", "text": encode(*instruction)})


async def _browser_to_guacd(receive, writer, on_input=None) -> str:
    """Guacamole instructions from the browser, unchanged. They are already
    correctly framed by the client library and Vigil must not touch them."""
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return "browser closed"
        if kind != "websocket.receive":
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text:
            continue
        if on_input is not None:
            on_input()
        writer.write(text.encode("utf-8"))
        await writer.drain()


async def _supervise(tasks) -> str:
    """Wait for the first task to finish and take its reason. Whatever ends the
    desktop, the others must not outlive it — ``run_browser`` cancels them."""
    done, _pending = await asyncio.wait(list(tasks), return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        exc = task.exception()
        if exc is not None:
            # The exception's name is all the log gets: an instruction or a
            # parameter value must never reach it.
            logger.warning("rdp session ended: %s", type(exc).__name__)
            return "desktop error"
        if isinstance(task.result(), str):
            return task.result()
    return "closed"


def _recording_for(session) -> str:
    """The recording's path, on the tiers that record. Business only, and guacd
    writes it inside its own container under ``VIGIL_RDP_RECORDING_PATH`` — the
    shared volume is what lets Vigil serve it afterwards.

    The feature check reads the license from the database, so callers on a
    socket reach this through ``_recording`` below rather than calling it inline.
    """
    if not has_feature("audit_log"):
        return ""
    # Unguessable, like a shell recording: MEDIA_ROOT is served as /media/ when
    # DEBUG is on, and session ids are listed in the API.
    import secrets

    return f"recordings/rdp/{session.id}-{secrets.token_hex(16)}"


async def _recording(session) -> str:
    """Whether this desktop is recorded, decided off the event loop."""
    from asgiref.sync import sync_to_async

    return await sync_to_async(_recording_for, thread_sensitive=True)(session)


async def _save_recording(session, recording: str) -> None:
    """Record the path on the row, before a single desktop pixel is relayed.

    A desktop that dies in its first second still produced a recording up to its
    last moment, and an operator looking for it afterwards must be able to find
    it — which is what the download view's ``session.recording`` lookup is.

    The model is imported here rather than at module top: this coroutine runs on
    a socket, where a database row belongs on a thread, not in an import graph.
    """
    from asgiref.sync import sync_to_async

    from .models import RemoteSession

    if not recording:
        return
    session.recording = recording
    await sync_to_async(RemoteSession.objects.filter(pk=session.pk)
                        .update, thread_sensitive=True)(recording=recording)
