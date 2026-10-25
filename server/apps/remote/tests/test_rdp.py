"""Web RDP (QA-59): the Guacamole codec, the one-shot bridge, and a desktop
session driven end to end with a fake guacd.

What lives in ``apps/remote/rdp.py`` is protocol-shaped rather than
product-shaped, so it is tested as protocol: instructions are compared byte for
byte, the handshake is checked against what a fake guacd says it received in the
order it received it, and the bridge's peer check is exercised by making guacd's
name resolve somewhere else.

The end-to-end test has the least code and carries the most weight: a browser,
the real ``browser_socket``, a fake guacd that dials the bridge the way the real
one does, and a pump standing in for the agent. It passes only if the desktop's
instructions arrive *and* the Windows password appears in no log record at any
level and on no bus key — the property this feature buys the right to exist.
"""

import asyncio
import base64
import json
import shutil
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.remote.recording import recording_allowed

from .. import bus, rdp, ws
from ..models import RemoteSession
from .asgi_harness import connect

HOST_TOKEN = "h" * 64
PASSWORD = "S3cr3t-Windows-Pass"

#: The parameter list a real guacd 1.5 answers ``select,rdp`` with, trimmed to
#: what this feature sends plus the recording trio. Its order is the contract:
#: ``connect`` carries one value per name, positionally.
FAKE_ARGS = ["args", "VERSION_1_5_0", "hostname", "port", "username", "password",
             "domain", "security", "ignore-cert", "width", "height", "dpi",
             "resize-method", "recording-path", "recording-name",
             "create-recording-path"]


def encode(*elements) -> str:
    """The test's own encoder, so a bug in ``rdp.encode`` cannot hide behind a
    fake that was built with it."""
    return ",".join(f"{len(str(el))}.{el}" for el in elements) + ";"


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def _sync(fn):
    from asgiref.sync import sync_to_async

    return await sync_to_async(fn, thread_sensitive=True)()


class _FakeGuacd:
    """A guacd that answers the handshake and then relays like the real one.

    It records every instruction it is sent, can push instructions at the
    browser's side of the relay, and — because a real guacd connects straight
    back to the bridge Vigil named in ``connect`` — dials that port itself. That
    is how the peer check gets exercised against a connection from a real socket
    rather than a mock of one.

    *script* is what it does with its own connection to the bridge after
    ``ready``: ``("out", bytes)`` sends those bytes once, which is how a test
    sees what the agent would receive; ``("expect", bytes)`` waits for them from
    the bridge and then answers on the browser's channel.
    """

    def __init__(self, *, args=None, error: str | None = None, script=None):
        self.args = list(FAKE_ARGS if args is None else args)
        self.error = error
        self.script = script
        #: Every complete instruction, as elements, in arrival order.
        self.received: list[list[str]] = []
        self.connect_values: list[str] | None = None
        self.bridge_port: int | None = None
        #: Bytes the fake read off its connection to the bridge.
        self.from_bridge: list[bytes] = []
        self.handshake_complete = False
        self._ready = asyncio.Event()
        self._to_client: asyncio.Queue = asyncio.Queue()
        self._server = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, host="127.0.0.1", port=0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await _quietly(self._server.wait_closed())
            self._server = None

    def send_to_client(self, *instruction) -> None:
        """Queue one instruction for the browser's side of the relay."""
        self._to_client.put_nowait(encode(*instruction))

    async def wait_for_handshake(self, timeout: float = 5.0) -> bool:
        return await _wait_for(lambda: self.handshake_complete, timeout)

    async def _serve(self, reader, writer) -> None:
        relaying = asyncio.create_task(self._pump_to_client(writer))
        decoder = rdp.Decoder()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                for instruction in decoder.feed(chunk.decode("utf-8")):
                    await self._handle(instruction, writer)
        except (ConnectionError, asyncio.IncompleteReadError, rdp.RdpError):
            pass
        finally:
            relaying.cancel()
            await asyncio.gather(relaying, return_exceptions=True)
            writer.close()

    async def _pump_to_client(self, writer) -> None:
        while True:
            item = await self._to_client.get()
            writer.write(item.encode("utf-8"))
            await writer.drain()

    async def _handle(self, parts: list[str], writer) -> None:
        self.received.append(parts)
        opcode = parts[0]
        if opcode == "select":
            writer.write(encode(*self.args).encode("utf-8"))
        elif opcode == "connect":
            self.connect_values = parts[1:]
            try:
                self.bridge_port = int(self.connect_values[2])
            except (IndexError, ValueError):
                self.bridge_port = None
            if self.error:
                writer.write(encode("error", self.error, "0").encode("utf-8"))
            else:
                writer.write(encode("ready", "fake-connection-id").encode("utf-8"))
            await writer.drain()
            self.handshake_complete = True
            self._ready.set()
            if not self.error and self.bridge_port is not None:
                asyncio.get_running_loop().create_task(self._dial_bridge())
        else:
            await writer.drain()

    async def _dial_bridge(self) -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.bridge_port)
        except OSError:
            return
        reading = asyncio.create_task(self._read_bridge(reader))
        if self.script and self.script[0] == "out":
            # Bytes from guacd's RDP connection: what the agent has to see.
            writer.write(self.script[1])
            await writer.drain()
        elif self.script and self.script[0] == "expect":
            wanted = self.script[1]
            if not await _wait_for(lambda: wanted in b"".join(self.from_bridge), 5.0):
                writer.close()
                return
            self.send_to_client("sync", "answered", "0")
        try:
            await reading
        finally:
            writer.close()

    async def _read_bridge(self, reader) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                return
            self.from_bridge.append(data)


# ---------------------------------------------------------------------------
# The codec
# ---------------------------------------------------------------------------

class CodecTests(TransactionTestCase):
    def test_encode_frames_each_element_by_its_length_and_ends_with_a_semicolon(self):
        self.assertEqual(rdp.encode("size", "1024", "768"), "4.size,4.1024,3.768;")

    def test_a_non_ascii_value_is_counted_in_code_points_not_bytes(self):
        # "é" is two UTF-8 bytes and one code point; 𝄞 is four bytes and — the
        # case a JS client gets wrong — one code point of two UTF-16 units.
        self.assertEqual(rdp.encode("x", "é"), "1.x,1.é;")
        self.assertEqual(rdp.encode("title", "\U0001d11e"), "5.title,1.\U0001d11e;")

    def test_an_instruction_with_no_arguments_is_its_opcode_alone(self):
        # "audio" is Vigil saying it has no audio: the opcode with nothing after.
        self.assertEqual(rdp.encode("audio"), "5.audio;")

    def test_an_instruction_split_across_three_feeds_comes_out_whole(self):
        decoder = rdp.Decoder()
        wire = encode("sync", "1234", "0")
        self.assertEqual(decoder.feed(wire[:6]), [])
        self.assertEqual(decoder.feed(wire[6:11]), [])
        self.assertEqual(decoder.feed(wire[11:]), [["sync", "1234", "0"]])

    def test_two_instructions_in_one_feed_come_out_in_order(self):
        decoder = rdp.Decoder()
        wire = encode("size", "800") + encode("mouse", "1", "2", "3")
        self.assertEqual(decoder.feed(wire), [["size", "800"], ["mouse", "1", "2", "3"]])

    def test_an_empty_value_survives_the_round_trip(self):
        # An empty element is guacd's "not specified"; dropping one would shift
        # every parameter that follows it in connect.
        decoder = rdp.Decoder()
        self.assertEqual(decoder.feed(encode("connect", "", "x", "")),
                         [["connect", "", "x", ""]])

    def test_a_length_that_does_not_match_its_value_is_refused(self):
        # 9.size claims nine characters; once nine have arrived, what follows
        # them is neither "," nor ";". A stream that lies about its own framing
        # cannot be re-synchronised, so it ends.
        decoder = rdp.Decoder()
        with self.assertRaises(rdp.RdpError):
            decoder.feed("9.size;abcdef")

    def test_a_length_that_is_not_a_number_is_refused(self):
        decoder = rdp.Decoder()
        with self.assertRaises(rdp.RdpError):
            decoder.feed("size,4.x;")

    def test_two_decoders_do_not_share_partial_state(self):
        # Two desktops parse at once, and a half-finished instruction from one
        # must not be prepended to the other's.
        first, second = rdp.Decoder(), rdp.Decoder()
        self.assertEqual(first.feed(encode("size", "1024")[:7]), [])
        self.assertEqual(second.feed(encode("sync", "1")), [["sync", "1"]])

    def test_the_byte_decoder_rejoins_a_multibyte_character_split_across_reads(self):
        text = rdp.InstructionText()
        wire = encode("name", "héllo").encode("utf-8")
        split = next(i for i in range(1, len(wire)) if wire[i] & 0xC0 == 0x80)
        self.assertEqual(text.feed(wire[:split]), [])
        self.assertEqual(text.feed(wire[split:]), [["name", "héllo"]])


# ---------------------------------------------------------------------------
# Handshake against a fake guacd
# ---------------------------------------------------------------------------

@override_settings(VIGIL_SESSION_BUS="memory", VIGIL_RDP_BRIDGE_HOST="web")
class HandshakeTests(TransactionTestCase):
    def setUp(self):
        bus.reset()
        self.params = {
            "hostname": "web", "port": "41234", "username": "alice",
            "password": PASSWORD, "domain": "CORP", "security": "any",
            "ignore-cert": "true", "width": 1280, "height": "800", "dpi": "96",
            "resize-method": "display-update",
            "recording-path": "/recordings", "recording-name": "sid-1",
            "create-recording-path": "true",
        }

    async def _handshake(self, guacd: _FakeGuacd, recording: bool, params=None, **kw):
        port = await guacd.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.addCleanup(_close_writer, writer)
        try:
            await rdp.handshake(reader, writer, params or dict(self.params),
                                recording=recording, **kw)
        finally:
            # Client first: since Python 3.12 Server.wait_closed() waits for
            # every connection, so stopping the fake with ours open never ends.
            _close_writer(writer)
            await _quietly_sync(guacd)

    async def test_rdp_is_selected_unless_told_otherwise(self):
        guacd = _FakeGuacd()
        await self._handshake(guacd, recording=False)
        self.assertEqual(guacd.received[0], ["select", "rdp"])

    async def test_a_mac_session_selects_vnc(self):
        guacd = _FakeGuacd()
        await self._handshake(guacd, recording=False, protocol="vnc")
        self.assertEqual(guacd.received[0], ["select", "vnc"])

    def test_the_protocol_follows_the_hosts_os(self):
        from types import SimpleNamespace as NS

        for os_name, want in (("macOS 15.1", "vnc"), ("Darwin 24.1.0", "vnc"),
                              ("Windows 11", "rdp"), ("Ubuntu 24.04", "rdp"), ("", "rdp")):
            self.assertEqual(rdp.desktop_protocol(NS(host=NS(os=os_name))), want, os_name)

    async def test_the_connect_carries_one_value_per_name_in_guacds_own_order(self):
        guacd = _FakeGuacd()
        await self._handshake(guacd, recording=False)
        values = guacd.connect_values
        names = FAKE_ARGS[1:]
        self.assertEqual(len(values), len(names))
        # The echoed version is what lets guacd line its parameters up with ours.
        self.assertEqual(values[0], "VERSION_1_5_0")
        by_name = dict(zip(names, values))
        self.assertEqual(by_name["hostname"], "web")
        self.assertEqual(by_name["port"], "41234")
        self.assertEqual(by_name["username"], "alice")
        self.assertEqual(by_name["password"], PASSWORD)
        self.assertEqual(by_name["domain"], "CORP")
        self.assertEqual(by_name["security"], "any")
        self.assertEqual(by_name["ignore-cert"], "true")
        self.assertEqual(by_name["resize-method"], "display-update")

    async def test_the_client_describes_its_screen_audio_video_image_and_timezone(self):
        guacd = _FakeGuacd()
        await self._handshake(guacd, recording=False)
        sent = {parts[0]: parts for parts in guacd.received}
        self.assertEqual(sent["size"], ["size", "1280", "800", "96"])
        self.assertEqual(sent["audio"], ["audio"])
        self.assertIn("image/png", sent["image"])
        self.assertIn("image/jpeg", sent["image"])
        self.assertEqual(sent["timezone"], ["timezone", "UTC"])

    async def test_a_screen_size_outside_the_range_is_clamped(self):
        guacd = _FakeGuacd()
        params = {**self.params, "width": 99999, "height": 1, "dpi": "0"}
        await self._handshake(guacd, recording=False, params=params)
        sent = next(parts for parts in guacd.received if parts[0] == "size")
        self.assertEqual(sent, ["size", "3840", "240", "72"])

    async def test_a_screen_size_that_is_not_a_number_falls_back_to_the_default(self):
        guacd = _FakeGuacd()
        params = {**self.params, "width": None, "height": "lots"}
        await self._handshake(guacd, recording=False, params=params)
        sent = next(parts for parts in guacd.received if parts[0] == "size")
        self.assertEqual(sent, ["size", "1024", "768", "96"])

    async def test_the_recording_parameters_are_sent_only_when_recording(self):
        names = FAKE_ARGS[1:]

        off_guacd = _FakeGuacd()
        await self._handshake(off_guacd, recording=False)
        off = dict(zip(names, off_guacd.connect_values))
        self.assertEqual(off["recording-path"], "")
        self.assertEqual(off["recording-name"], "")
        self.assertEqual(off["create-recording-path"], "")

        on_guacd = _FakeGuacd()
        await self._handshake(on_guacd, recording=True)
        on = dict(zip(names, on_guacd.connect_values))
        self.assertEqual(on["recording-path"], "/recordings")
        self.assertEqual(on["recording-name"], "sid-1")
        self.assertEqual(on["create-recording-path"], "true")

    async def test_an_older_guacd_gets_the_security_value_it_understands(self):
        # NLA spellings such as "any" arrived with protocol 1.4.0, so the echoed
        # version decides what we send for security before it is sent at all.
        guacd = _FakeGuacd(args=["args", "VERSION_1_3_0"] + FAKE_ARGS[2:])
        await self._handshake(guacd, recording=False)
        by_name = dict(zip(FAKE_ARGS[2:], guacd.connect_values[1:]))
        self.assertEqual(by_name["security"], "rdp")

    async def test_an_error_from_guacd_ends_the_handshake_as_an_rdp_error(self):
        guacd = _FakeGuacd(error="RDP connect failed")
        with self.assertRaises(rdp.RdpError) as caught:
            await self._handshake(guacd, recording=False)
        self.assertIn("RDP connect failed", str(caught.exception))

    async def test_a_guacd_that_never_answers_times_out(self):
        # A listener that accepts and says nothing: the handshake gives up on its
        # own clock rather than holding a browser open indefinitely.
        held: list = []

        async def say_nothing(reader, writer):
            # Keep the connection open and silent; returning would close it.
            held.append(writer)
            await reader.read()

        quiet = await asyncio.start_server(say_nothing, host="127.0.0.1", port=0)
        self.addCleanup(quiet.close)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", quiet.sockets[0].getsockname()[1])
        with patch.object(rdp, "HANDSHAKE_SECONDS", 0.2), \
                self.assertRaises(rdp.RdpError) as caught:
            await rdp.handshake(reader, writer, dict(self.params), recording=False)
        _close_writer(writer)
        self.assertIn("handshake", str(caught.exception))


# ---------------------------------------------------------------------------
# The one-shot bridge
# ---------------------------------------------------------------------------

@override_settings(VIGIL_SESSION_BUS="memory")
class BridgeTests(TransactionTestCase):
    """One accepted peer, from one address, for a fixed window.

    The bridge listens on every interface — guacd reaches this container by
    name — so all three of those limits are the difference between a private
    pipe and an open door into somebody's session.
    """

    SID = "6f1e9d4c-7a3b-4c2d-9e1f-0a5b8c7d6e3f"

    def setUp(self):
        bus.reset()

    async def _bridge(self, guacd_host="localhost", timeout: float = 5.0):
        bridge = rdp.OneShotBridge(self.SID, guacd_host, timeout=timeout)
        await bridge.start()
        self.addCleanup(bridge.close)
        return bridge

    async def _connect(self, bridge, host: str = "127.0.0.1"):
        return await asyncio.open_connection(host, bridge.port)

    def b2a(self) -> list[dict]:
        return bus.peek(bus.B2A_KEY.format(sid=self.SID))

    async def test_a_peer_where_guacd_resolved_is_accepted_and_its_bytes_are_piped(self):
        bridge = await self._bridge(guacd_host="localhost")
        _reader, writer = await self._connect(bridge)
        writer.write(b"X" * 5000)
        await writer.drain()
        self.assertTrue(await _wait_for(lambda: len(self.b2a()) >= 1))
        item = self.b2a()[0]
        self.assertEqual(item["t"], "in")
        self.assertEqual(base64.b64decode(item["d"]), b"X" * 5000)
        self.assertEqual(bridge.peer, "127.0.0.1")
        _close_writer(writer)

    async def test_a_peer_where_guacd_did_not_resolve_is_hung_up_before_a_byte(self):
        with patch.object(rdp.socket, "getaddrinfo",
                          return_value=[(socket.AF_INET, 1, 6, "", ("10.9.9.9", 0))]):
            bridge = await self._bridge(guacd_host="guacd")
        reader, writer = await self._connect(bridge)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5.0), b"",
                         "a foreign peer was left connected")
        self.assertEqual(bridge.refused, 1)
        self.assertIsNone(bridge.peer)
        self.assertEqual(self.b2a(), [])
        _close_writer(writer)

    async def test_a_second_connection_is_refused_even_from_the_right_address(self):
        bridge = await self._bridge(guacd_host="localhost")
        _reader, first = await self._connect(bridge)
        first.write(b"x")
        await first.drain()
        self.assertTrue(await _wait_for(lambda: bridge.peer is not None))

        reader2, second = await self._connect(bridge)
        self.assertEqual(await asyncio.wait_for(reader2.read(), 5.0), b"")
        self.assertEqual(bridge.peer, "127.0.0.1")
        _close_writer(first)
        _close_writer(second)

    async def test_the_listener_is_gone_once_its_window_closes(self):
        # 30 s is long enough for a handshake and nothing else: after that the
        # port must not be held open for the length of a session.
        bridge = await self._bridge(guacd_host="localhost", timeout=0.2)
        port = bridge.port
        self.assertTrue(await _wait_for(lambda: bridge._server is None))
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_an_unresolvable_guacd_name_admits_no_connection_at_all(self):
        # Failing closed: if we cannot tell where guacd is, nothing connects.
        with patch.object(rdp.socket, "getaddrinfo",
                          side_effect=socket.gaierror("no such name")):
            bridge = await self._bridge(guacd_host="guacd-missing")
        reader, writer = await self._connect(bridge)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5.0), b"")
        self.assertEqual(bridge.refused, 1)
        _close_writer(writer)

    async def test_bytes_the_agent_sent_reach_guacds_connection(self):
        bridge = await self._bridge(guacd_host="localhost")
        reader, writer = await self._connect(bridge)
        self.assertTrue(await _wait_for(lambda: bridge.peer is not None))
        piping = asyncio.create_task(bridge.pipe())
        self.addCleanup(piping.cancel)
        bus.push_sync(bus.A2B_KEY.format(sid=self.SID),
                      {"t": "out", "d": base64.b64encode(b"RDP-bytes").decode()})
        self.assertEqual(await asyncio.wait_for(reader.read(1024), 5.0), b"RDP-bytes")
        _close_writer(writer)

    async def test_a_close_on_the_bus_ends_the_pipe(self):
        bridge = await self._bridge(guacd_host="localhost")
        bus.push_sync(bus.A2B_KEY.format(sid=self.SID),
                      {"t": "close", "reason": "browser closed"})
        await asyncio.wait_for(bridge.pipe(), 5.0)


# ---------------------------------------------------------------------------
# A desktop session, end to end
# ---------------------------------------------------------------------------

@override_settings(VIGIL_SESSION_BUS="memory", VIGIL_RDP_BRIDGE_HOST="web")
class _DesktopCase(TransactionTestCase):
    """Browser → ``browser_socket`` → relay → fake guacd, over the memory bus."""

    def setUp(self):
        bus.reset()
        self.host = Host.objects.create(
            hostname="win-box", agent_token=HOST_TOKEN, status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED, agent_allow_remote_desktop=True)
        self.user = self._user("alice")
        self.session = RemoteSession.objects.create(
            host=self.host, user=self.user, username="alice", kind="rdp", nonce="n1")

    def _user(self, name):
        user = get_user_model().objects.create_user(username=name, password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        return user

    async def cookie_for(self, user) -> str:
        await _sync(lambda: self.client.force_login(user))
        value = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        return f"{settings.SESSION_COOKIE_NAME}={value}"

    async def browser(self, cookie=None):
        if cookie is None:
            cookie = await self.cookie_for(self.user)
        return await connect(ws.websocket_app, f"/ws/session/{self.session.id}/",
                             cookie=cookie)

    async def guacd(self, **kwargs) -> _FakeGuacd:
        """A fake guacd on loopback, with the settings pointing at it."""
        guacd = _FakeGuacd(**kwargs)
        port = await guacd.start()
        self.addCleanup(_quietly_sync, guacd)
        patcher = override_settings(VIGIL_GUACD_ADDR=f"127.0.0.1:{port}")
        patcher.enable()
        self.addCleanup(patcher.disable)
        return guacd

    async def login(self, conn, **over) -> None:
        await conn.send_json({"t": "rdp_login", "username": "alice",
                              "password": PASSWORD, "domain": "",
                              "width": 1024, "height": 768, "dpi": 96, **over})

    async def send_instruction(self, conn, *elements) -> None:
        """One Guacamole instruction, as the browser's client library would send it."""
        await conn._wire.to_app.put({"type": "websocket.receive",
                                     "text": encode(*elements)})

    def refresh(self) -> RemoteSession:
        return RemoteSession.objects.get(pk=self.session.pk)

    async def close_code(self, conn) -> int | None:
        await conn.drain_to_close(timeout=5)
        await conn.settle()
        return conn.closed_code


class DesktopSessionTests(_DesktopCase):
    async def test_the_whole_path_relays_and_leaves_the_password_in_no_log(self):
        guacd = await self.guacd()
        records = _capture_logs(self)
        browser = await self.browser()
        self.assertTrue(await browser.answered())
        await self.login(browser)

        self.assertTrue(await guacd.wait_for_handshake(),
                        f"guacd received {guacd.received}")
        values = dict(zip(FAKE_ARGS[2:], guacd.connect_values[1:]))
        # The password reached the one place it is allowed to be.
        self.assertEqual(values["password"], PASSWORD)
        self.assertEqual(values["hostname"], "web")
        self.assertEqual(int(values["port"]), guacd.bridge_port)

        # guacd → browser: a drawing instruction from the desktop.
        guacd.send_to_client("sync", "1234", "0")
        self.assertEqual(await browser.receive_text(), encode("sync", "1234", "0"))

        # browser → guacd: a mouse move, on through the relay.
        await self.send_instruction(browser, "mouse", "1", "40", "60", "1")
        self.assertTrue(await _wait_for(
            lambda: any(p[0] == "mouse" for p in guacd.received)),
            f"guacd received {guacd.received}")

        await browser.close()
        session = await _sync(self.refresh)

        joined = "\n".join(record.getMessage() + repr(record.args) for record in records)
        self.assertNotIn(PASSWORD, joined)
        for item in self._bus_items():
            self.assertNotIn(PASSWORD, json.dumps(item))
        self.assertEqual(session.state, RemoteSession.State.CLOSED)

    async def test_guacds_connection_to_the_bridge_is_piped_to_the_agent(self):
        # All four corners: browser, Vigil's relay, a fake guacd that dials the
        # bridge it was given, and a pump standing in for the agent's socket.
        hello = b"\x03\x00\x00\x13"
        guacd = await self.guacd(script=("out", hello))
        browser = await self.browser()
        await self.login(browser)
        self.assertTrue(await guacd.wait_for_handshake())

        b2a = bus.B2A_KEY.format(sid=str(self.session.id))
        item = await bus.pop(b2a, 5.0)
        self.assertEqual(item["t"], "in")
        self.assertEqual(base64.b64decode(item["d"]), hello)
        await browser.close()

    async def test_the_agents_bytes_reach_guacd_over_the_bridge(self):
        wanted = b"\x03\x00\x00\x13nego"
        guacd = await self.guacd(script=("expect", wanted))
        browser = await self.browser()
        await self.login(browser)
        self.assertTrue(await guacd.wait_for_handshake())

        bus.push_sync(bus.A2B_KEY.format(sid=str(self.session.id)),
                      {"t": "out", "d": base64.b64encode(wanted).decode()})
        self.assertTrue(await _wait_for(lambda: b"".join(guacd.from_bridge) == wanted),
                        f"guacd read {guacd.from_bridge}")
        # ...and whatever guacd answers for it still gets to the browser.
        self.assertEqual(await browser.receive_text(), encode("sync", "answered", "0"))
        await browser.close()

    async def test_the_guacamole_subprotocol_is_echoed_back_to_the_browser(self):
        await self.guacd()
        conn = await _connect_with_subprotocols(
            ws.websocket_app, f"/ws/session/{self.session.id}/",
            cookie=await self.cookie_for(self.user), subprotocols=["guacamole"])
        self.assertTrue(await conn.answered())
        accepted = next(event for event in conn._wire.sent
                        if event["type"] == "websocket.accept")
        self.assertEqual(accepted.get("subprotocol"), "guacamole")
        await conn.close()

    async def test_a_business_session_hands_the_recording_to_guacd_and_the_row(self):
        guacd = await self.guacd()
        with self.settings(MEDIA_ROOT=self._tmp_media()), \
                patch("apps.remote.rdp.has_feature", return_value=True):
            browser = await self.browser()
            await self.login(browser)
            self.assertTrue(await guacd.wait_for_handshake())
            session = await _sync(self.refresh)
            await browser.close()

        values = dict(zip(FAKE_ARGS[2:], guacd.connect_values[1:]))
        self.assertEqual(values["recording-path"], "/recordings")
        # Starts with the session id, then a random part nobody can guess.
        self.assertTrue(values["recording-name"].startswith(f"{self.session.id}-"))
        self.assertGreater(len(values["recording-name"]), len(str(self.session.id)) + 16)
        self.assertEqual(values["create-recording-path"], "true")
        # Relative to MEDIA_ROOT, which is what the existing download view
        # resolves a session's recording against.
        self.assertEqual(session.recording, f"recordings/rdp/{values['recording-name']}")

    async def test_a_community_session_asks_guacd_for_no_recording(self):
        guacd = await self.guacd()
        with patch("apps.remote.rdp.has_feature", return_value=False):
            browser = await self.browser()
            await self.login(browser)
            self.assertTrue(await guacd.wait_for_handshake())
            session = await _sync(self.refresh)
            await browser.close()
        values = dict(zip(FAKE_ARGS[2:], guacd.connect_values[1:]))
        self.assertEqual(values["recording-path"], "")
        self.assertEqual(session.recording, "")

    async def test_the_password_is_not_left_reachable_after_the_handshake(self):
        await self.guacd()
        browser = await self.browser()
        await self.login(browser)
        await asyncio.sleep(0.2)
        await browser.close()
        # The login dict is a local, so the only lasting trace would be module
        # state — which is exactly where a careless implementation keeps it.
        held = [name for name in vars(rdp) if PASSWORD in str(getattr(rdp, name))]
        self.assertEqual(held, [])

    def _bus_items(self) -> list[dict]:
        items: list[dict] = []
        for key in (bus.B2A_KEY, bus.A2B_KEY, bus.WAKE_KEY, bus.BROWSER_KEY,
                    bus.AGENT_KEY):
            items.extend(bus.peek(key.format(sid=str(self.session.id),
                                             host_id=str(self.host.id))))
        return items

    def _tmp_media(self) -> str:
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


class DesktopRefusalTests(_DesktopCase):
    async def test_a_server_with_no_guacd_configured_closes_4503(self):
        with self.settings(VIGIL_GUACD_ADDR=""):
            browser = await self.browser()
            await self.login(browser)
            code = await self.close_code(browser)
        self.assertEqual(code, 4503)
        session = await _sync(self.refresh)
        self.assertEqual(session.close_reason, "Web RDP is not configured on this server")
        self.assertEqual(session.state, RemoteSession.State.CLOSED)

    async def test_an_unreachable_guacd_closes_4503(self):
        # A port nothing listens on: right setting, service not running. The
        # operator's mistake is different but the answer they need is the same.
        with self.settings(VIGIL_GUACD_ADDR="127.0.0.1:1"):
            browser = await self.browser()
            await self.login(browser)
            code = await self.close_code(browser)
        self.assertEqual(code, 4503)

    async def test_no_login_frame_closes_4400(self):
        browser = await self.browser()
        with patch.object(rdp, "LOGIN_SECONDS", 0.2):
            code = await self.close_code(browser)
        self.assertEqual(code, 4400)

    async def test_a_frame_that_is_not_the_login_closes_4400(self):
        browser = await self.browser()
        with patch.object(rdp, "LOGIN_SECONDS", 2.0):
            await browser.send_json({"t": "in",
                                     "d": base64.b64encode(b"ls").decode()})
            code = await self.close_code(browser)
        self.assertEqual(code, 4400)

    async def test_a_login_without_a_username_closes_4400(self):
        browser = await self.browser()
        with patch.object(rdp, "LOGIN_SECONDS", 2.0):
            await browser.send_json({"t": "rdp_login", "username": "",
                                     "password": PASSWORD})
            code = await self.close_code(browser)
        self.assertEqual(code, 4400)

    async def test_a_browser_that_hangs_up_during_the_wait_is_not_an_error(self):
        browser = await self.browser()
        with patch.object(rdp, "LOGIN_SECONDS", 2.0):
            await browser.close()
        self.assertEqual((await _sync(self.refresh)).state, RemoteSession.State.CLOSED)

    async def test_a_desktop_row_never_reaches_the_terminal_recorder(self):
        # A desktop's bytes are Guacamole instructions, not a terminal stream: a
        # .cast of them would be a file nothing can play back. The branch in
        # browser_socket is what keeps them apart.
        with patch("apps.remote.recording.has_feature", return_value=True):
            self.assertFalse(recording_allowed(RemoteSession.Kind.RDP))
            self.assertTrue(recording_allowed(RemoteSession.Kind.SHELL))

    async def test_the_terminal_path_is_untouched_by_the_branch(self):
        await _sync(lambda: RemoteSession.objects.filter(pk=self.session.pk)
                    .update(kind="shell"))
        browser = await self.browser()
        self.assertEqual(await browser.receive_json(),
                         {"t": "status", "state": "waiting"})
        await browser.close()

    async def test_someone_elses_desktop_is_not_found(self):
        mallory = await _sync(lambda: get_user_model().objects.create_user(
            username="mallory", password="pw"))
        await _sync(lambda: UserProfile.objects.create(user=mallory, role=Role.ADMIN))
        browser = await self.browser(cookie=await self.cookie_for(mallory))
        self.assertEqual(await self.close_code(browser), 4404)

    async def test_a_second_browser_on_the_same_desktop_is_refused(self):
        first = await self.browser()
        self.assertTrue(await first.answered())
        second = await self.browser()
        self.assertEqual(await self.close_code(second), 4409)
        await first.close()


# ---------------------------------------------------------------------------
# The pieces no Python test can execute, read as source
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css" / "vigil.css"
COMPOSE = ROOT.parent / "docker-compose.yml"


def _read(path: Path) -> str:
    return path.read_text()


class ComposeTests(TransactionTestCase):
    """guacd is an optional service, and the two ways it could be wrong are both
    silent: a published port turns it into a proxy anyone can reach, and a
    missing volume means a Business session recorded nothing at all."""

    def setUp(self):
        self.compose = _read(COMPOSE)

    def test_guacd_is_opt_in_and_publishes_nothing(self):
        block = self._service("guacd")
        self.assertIn("guacamole/guacd:1.5.5", block)
        self.assertIn('profiles: ["rdp"]', block)
        self.assertNotIn("ports:", block)
        self.assertIn("restart: unless-stopped", block)

    def test_the_recording_volume_is_shared_by_both_containers(self):
        self.assertIn("rdp-recordings:/recordings", self._service("guacd"))
        self.assertIn("rdp-recordings:/app/media/recordings/rdp", self._service("web"))
        self.assertRegex(self.compose, r"(?m)^\s+rdp-recordings:\s*$")

    def test_the_web_container_is_told_where_guacd_is_and_what_to_call_itself(self):
        web = self._service("web")
        self.assertIn("VIGIL_GUACD_ADDR: ${VIGIL_GUACD_ADDR:-}", web)
        self.assertIn("VIGIL_RDP_BRIDGE_HOST: web", web)

    def test_the_documented_switch_is_in_the_header(self):
        self.assertIn("#   VIGIL_GUACD_ADDR=guacd:4822  — set with --profile rdp "
                      "to turn on Web RDP", self.compose)

    def _service(self, name: str) -> str:
        lines = self.compose.splitlines()
        start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() and not lines[i].startswith("    ")), len(lines))
        return "\n".join(lines[start:end])


class RdpBrowserTests(TransactionTestCase):
    def setUp(self):
        self.js = _strip_comments(_read(JS / "vigil-rdp.js"))

    def test_open_session_and_the_tunnel_are_defined(self):
        for fn in ("async function openRdpSession", "function VigilRdpTunnel"):
            self.assertIn(fn, self.js)

    def test_the_guacamole_parser_is_what_buffers_partial_instructions(self):
        # Reimplementing it badly is how a desktop starts drawing half a frame.
        self.assertIn("new Guacamole.Parser()", self.js)
        self.assertIn("parser.receive(", self.js)

    def test_outgoing_instruction_lengths_count_code_points(self):
        self.assertRegex(self.js, r"Array\.from\((?:value|String\([^)]*\))\)\.length")

    def test_the_websocket_negotiates_the_guacamole_subprotocol(self):
        self.assertIn("new WebSocket(url, 'guacamole')", self.js)

    def test_the_password_field_is_a_password_and_the_login_is_dropped_after_send(self):
        self.assertIn('type="password"', self.js)
        self.assertIn("login = null;", self.js)
        self.assertLess(self.js.index("t: 'rdp_login'"), self.js.index("login = null;"))

    def test_nothing_generated_carries_an_inline_handler(self):
        # The CSP has no 'unsafe-inline': an inline attribute on generated
        # markup is a button that does nothing, with no error to show for it.
        self.assertNotIn("onclick=", self.js)
        self.assertRegex(self.js, r"addEventListener\('click'")

    def test_the_credentials_never_reach_a_url(self):
        # Guacamole's own HTTP tunnels put their parameters in a query string,
        # which an access log would keep. This is why there is a custom tunnel.
        head = self.js[:self.js.index("socket.send(JSON.stringify")]
        self.assertNotIn(PASSWORD, head)
        for line in self.js.splitlines():
            if "password" in line.lower() and ("ws://" in line or "wss://" in line):
                self.fail(f"credentials in a URL: {line.strip()}")

    def test_input_is_wired_to_the_display_and_the_keyboard_is_detached_on_close(self):
        self.assertIn("new Guacamole.Mouse(", self.js)
        self.assertIn("client.sendMouseState", self.js)
        self.assertIn("new Guacamole.Keyboard(document)", self.js)
        self.assertIn("client.sendKeyEvent", self.js)
        self.assertIn("keyboard.onkeydown = null;", self.js)
        self.assertIn("keyboard.onkeyup = null;", self.js)

    def test_the_display_and_resize_are_wired(self):
        self.assertIn("client.getDisplay().getElement()", self.js)
        self.assertIn("new ResizeObserver(", self.js)
        self.assertIn("client.sendSize(", self.js)

    def test_the_status_pill_tracks_the_tunnel_state(self):
        for text in ("Connecting…", "'Connected'", "'Closed'"):
            self.assertIn(text, self.js)
        self.assertIn("Guacamole.Tunnel.State.OPEN", self.js)
        self.assertIn("client.onerror", self.js)

    def test_the_assets_come_from_the_page_and_load_in_order(self):
        cards = _read(JS / "vigil-host-cards.js")
        self.assertRegex(cards, r"\bfunction loadRdpAssets\s*\(")
        order = [cards.index("src.dataset.guac"), cards.index("src.dataset.rdp")]
        self.assertEqual(order, sorted(order))
        self.assertIn("loadRdpAssets()", cards)

    def test_the_monitor_page_names_both_scripts(self):
        html = _read(ROOT / "templates" / "pages" / "_monitor.html")
        self.assertIn('data-guac="{% static \'js/vendor/guacamole-common.js\' %}"', html)
        self.assertIn('data-rdp="{% static \'js/vigil-rdp.js\' %}"', html)

    def test_the_dialog_and_the_display_are_styled(self):
        css = _read(CSS)
        self.assertIn(".rdp-modal", css)
        block = css[css.index(".modal.rdp-modal"):]
        self.assertIn("resize: both", block[:400])
        self.assertIn("flex-direction: column", block[:400])
        display = block[block.index(".rdp-display"):]
        self.assertIn("flex: 1", display)
        self.assertIn("overflow: hidden", display)
        self.assertIn("background: #000", display)


def _strip_comments(src: str) -> str:
    """JS with its comments blanked, one line per line.

    The ban being read here is on code, and this file's comments explain the ban
    — so scanning raw text reads the explanation as the offence. apps/hosts and
    test_connect_ui.py do the same for the same reason.
    """
    out, i, n = [], 0, len(src)
    in_line = in_block = False
    while i < n:
        ch, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        elif ch == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
        elif ch == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Small async helpers, so no test needs a server or a thread to look after one
# ---------------------------------------------------------------------------

async def _connect_with_subprotocols(app, path: str, cookie: str, subprotocols: list):
    """``asgi_harness.connect``, with the client offering a subprotocol — which
    is how a real Guacamole browser client asks for ``guacamole``."""
    conn = await connect(app, path, cookie=cookie, subprotocols=subprotocols)
    await conn.answered()
    return conn


def _close_writer(writer) -> None:
    try:
        writer.close()
    except (ConnectionError, OSError):
        pass


async def _quietly(coro) -> None:
    try:
        await coro
    except (ConnectionError, OSError):
        pass


async def _quietly_sync(guacd: _FakeGuacd) -> None:
    await guacd.stop()


def _capture_logs(case) -> list:
    """Every record any logger emits at DEBUG and up while the test runs.

    Unlike assertLogs this passes when nothing logs, which for a password
    check is the best outcome."""
    import logging

    records: list = []

    class _Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Keep(level=logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    case.addCleanup(root.removeHandler, handler)
    case.addCleanup(root.setLevel, previous)
    return records



class DesktopWatchdogTests(_DesktopCase):
    """A desktop gets the same limits as a shell: it opens when the agent does,
    and closes when idle (or at the time limit, or when the login ends)."""

    async def test_the_agent_opening_marks_the_session_open_and_idle_closes_it(self):
        guacd = await self.guacd()
        with patch.object(ws, "IDLE_SECONDS", 0.5), patch.object(ws, "WATCHDOG_TICK", 0.05):
            browser = await self.browser()
            self.assertTrue(await browser.answered())
            await self.login(browser)
            self.assertTrue(await guacd.wait_for_handshake())
            # The agent's socket opening, as agent_socket announces it.
            bus.push_sync(bus.A2B_KEY.format(sid=str(self.session.id)), {"t": "open"})
            opened = await _wait_for_state(self, "open")
            code = await self.close_code(browser)
        self.assertTrue(opened, "the session never opened")
        session = await _sync(self.refresh)
        self.assertEqual(session.close_reason, "idle timeout")
        self.assertEqual(code, 1000)


async def _wait_for_state(case, state: str, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if (await _sync(case.refresh)).state == state:
            return True
        await asyncio.sleep(0.02)
    return False
