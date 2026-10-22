"""The link that joins the dashboard to a relay: attestation headers, the ORM
bookkeeping, and — end to end against the real relay container code — the
forwarding of an agent request and of an agent WebSocket."""

import asyncio
import hashlib
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings

# The relay ships in this repo. It is not an installed package, so the end-to-end
# tests below run against the real container app by way of this path, which is
# why it goes on before the imports that use it.
sys.path.insert(0, str(Path(settings.BASE_DIR).parent / "relay"))

import aiohttp  # noqa: I001 — the sys.path line above has to come first
from aiohttp import web
from aiohttp.test_utils import TestServer
from django.test import TestCase
from django.utils.timezone import now

from vigil_relay.app import make_app
from vigil_relay.config import Config

from .link import LinkManager, Target, Uplink, forward_headers, load_targets, record_status
from .models import Relay, set_key
from .trust import link_token

KEY = "k"

#: Two round trips through a real relay over real sockets, with room to spare. A
#: fixed ceiling is what makes a stalled pipe fail the test instead of pass late.
_RECEIVE_TIMEOUT = 20.0


def _sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ForwardHeadersTests(unittest.TestCase):
    def test_forward_headers_strip_agent_supplied_vigil_headers(self):
        out = forward_headers(
            {"X-Vigil-Link": "forged", "x-vigil-agent-ip": "1.2.3.4",
             "Authorization": "Bearer t", "Host": "evil"},
            "r1", "198.51.100.7")
        low = {k.lower(): v for k, v in out.items()}
        self.assertEqual(low["x-vigil-link"], link_token())
        self.assertEqual(low["x-vigil-relay"], "r1")
        self.assertEqual(low["x-vigil-agent-ip"], "198.51.100.7")
        self.assertEqual(low["authorization"], "Bearer t")
        self.assertEqual(low["host"], "localhost")

    def test_forward_headers_rejects_a_smuggled_attestation_name(self):
        # Anything in our namespace is dropped by prefix, not by a name list: an
        # X-Vigil-* the link does not know yet must not be able to arrive upstream
        # carrying an agent's words. (This test and the one above fail together;
        # this one names the prefix rule specifically.)
        out = forward_headers({"X-Vigil-Something-New": "forged", "Accept": "*/*"}, "r1", "")
        self.assertNotIn("x-vigil-something-new", {k.lower() for k in out})
        self.assertIn("accept", {k.lower() for k in out})

    def test_forward_headers_drops_the_agents_own_copies(self):
        # A smuggled Content-Length would fight the one aiohttp computes for the
        # upstream request; the agent's Content-Type is the agent's business.
        out = forward_headers({"Content-Length": "13", "Connection": "keep-alive",
                               "Upgrade": "websocket", "Transfer-Encoding": "chunked",
                               "X-Vigil-Whatever": "x", "Content-Type": "application/json"},
                              "r1", "")
        low = {k.lower() for k in out}
        self.assertNotIn("content-length", low)
        self.assertNotIn("connection", low)
        self.assertNotIn("upgrade", low)
        self.assertNotIn("transfer-encoding", low)
        self.assertNotIn("x-vigil-whatever", low)
        self.assertIn("content-type", low)


class TargetTests(TestCase):
    def test_load_targets_skips_disabled_builtin_and_keyless(self):
        keyed = Relay.objects.create(name="edge", url="https://relay.example.com/t/acme/",
                                     kind=Relay.Kind.REMOTE, enabled=True)
        set_key(keyed, "vrk_secret")
        keyed.save()

        Relay.objects.create(name="no-key", url="https://nokey.example.com",
                             kind=Relay.Kind.REMOTE, enabled=True)
        Relay.objects.create(name="off", url="https://off.example.com",
                             kind=Relay.Kind.REMOTE, enabled=False)
        Relay.objects.create(name="builtin", url="", kind=Relay.Kind.BUILTIN, enabled=True)

        targets = load_targets()
        self.assertEqual([t.id for t in targets], [str(keyed.id)])
        target = targets[0]
        # The uplink path is appended to what load_targets hands out, so the
        # stored trailing "/" has to come off here or it doubles.
        self.assertEqual(target.url, "https://relay.example.com/t/acme")
        self.assertEqual(target.key, "vrk_secret")
        self.assertEqual(target.key_sha256, _sha("vrk_secret"))

    def test_record_status(self):
        relay = Relay.objects.create(name="edge", url="https://relay.example.com",
                                     kind=Relay.Kind.REMOTE)
        seen = now() - timedelta(days=3)
        Relay.objects.filter(pk=relay.pk).update(last_seen_at=seen)

        record_status(str(relay.pk), connected=True)
        relay.refresh_from_db()
        self.assertTrue(relay.connected)
        self.assertGreater(relay.last_seen_at, seen)
        self.assertEqual(relay.last_error, "")

        record_status(str(relay.pk), connected=False, error="OSError: boom")
        relay.refresh_from_db()
        self.assertFalse(relay.connected)
        # A failed attempt never touches last_seen_at — that is "the last time we
        # actually reached it", which the UI sorts on.
        self.assertIsNotNone(relay.last_seen_at)
        self.assertEqual(relay.last_error, "OSError: boom")


class _FakeUplink:
    """Stand-in for Uplink in the manager test: records its lifecycle only."""

    def __init__(self, record, target):
        self.record = record
        self.target = target
        record["starts"].append(target.id)

    async def run(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Appending here rather than in a done-callback matters: a callback
            # queued by the cancellation is not due until the task that cancelled
            # us yields, so the manager's next reload pass would not see it yet.
            self.record["cancels"].append(self.target.id)
            raise


class LinkManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_restarts_on_key_change(self):
        first = Target(id="r1", url="https://a.example.com", key="k1", key_sha256="a" * 64)
        rotated = Target(id="r1", url="https://a.example.com", key="k2", key_sha256="b" * 64)
        loads = [[first], [rotated]]

        def loader():
            return loads.pop(0) if loads else [rotated]

        record = {"starts": [], "cancels": []}
        manager = LinkManager(loader=loader,
                              uplink_factory=lambda target: _FakeUplink(record, target))
        with patch("apps.relays.link.RELOAD_SECONDS", 0.01):
            run = asyncio.create_task(manager.run())
            self.addCleanup(_retire, run)

            await _until(lambda: (record["starts"] == ["r1", "r1"]
                                  and record["cancels"] == ["r1"]))
            self.assertEqual(manager.targets["r1"].key_sha256, "b" * 64)
            await manager.stop()


async def _stop_all(servers):
    for server in servers:
        await server.close()


async def _retire(task):
    """Cancel and wait. A cleanup that only asked for the cancel leaves the
    task's teardown — closing its sockets — to run inside the next test."""
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _until(predicate, attempts: int = 600):
    """Wait for an event raised by another task. Nothing here compares clocks."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# --------------------------------------------------------------------------
# End to end: the real relay app, a fake Vigil web service, the real Uplink.
# No database — the link's ORM half is TargetTests' business.


class Recorder(list):
    """Stands in for ``record_status``, which the link calls with keywords —
    a bare list's append cannot take them."""

    def __call__(self, relay_id: str, *, connected: bool, error: str = "") -> None:
        self.append({"relay_id": relay_id, "connected": connected, "error": error})


class LinkEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue_dir = tempfile.mkdtemp(prefix="vigil-link-test-")
        self.addCleanup(shutil.rmtree, self.queue_dir, True)

        self.relay = await _serve(make_app(Config(tenants={"default": _sha(KEY)},
                                                  queue_dir=self.queue_dir)))
        self.upstream = await _serve(_web_service())
        # The relay answers the health check itself, so it is not a link path.
        self.agent_path = "/api/v1/checkin"

        self.recorder = Recorder()
        self.uplink = Uplink(
            Target(id="r1", url=str(self.relay.make_url("")).rstrip("/"),
                   key=KEY, key_sha256=_sha(KEY)),
            upstream=str(self.upstream.make_url("")).rstrip("/"),
            status=self.recorder)
        # The link's own retry loop is the only thing that decides when an uplink
        # is ready; the test never sleeps a fixed time waiting for it.
        self.link_task = asyncio.create_task(self.uplink.run())
        await _until(lambda: bool(self.recorder) and self.recorder[-1]["connected"])
        # Retire the link first (cleanups run LIFO), cancelling *and* awaiting it
        # so it closes its uplink; only then stop the servers. The other way
        # round, the relay's shutdown waits out aiohttp's 60 s grace for the
        # still-open uplink socket, once per test.
        self.addCleanup(_stop_all, [self.upstream, self.relay])
        self.addCleanup(_retire, self.link_task)

    async def test_agent_request_reaches_upstream_with_link_headers(self):
        status, body = await _request(
            "POST", str(self.relay.make_url(self.agent_path)), b'{"hostname":"h1"}',
            {"Content-Type": "application/json",
             "X-Vigil-Link": "forged", "X-Vigil-Agent-IP": "1.2.3.4"})

        self.assertEqual(status, 200)
        self.assertEqual(body["method"], "POST")
        self.assertEqual(body["path"], self.agent_path)
        self.assertEqual(body["body"], '{"hostname":"h1"}')
        # Only the web service may attest an agent. What it sees is our token and
        # none of what the agent forged; the agent's IP travels as our own header,
        # taken from the frame — never from the agent.
        headers = body["headers"]
        self.assertEqual(headers["x-vigil-link"], link_token())
        self.assertEqual(headers["x-vigil-relay"], "r1")
        # The agent's forged IP never arrives; the attested one is the address
        # the relay saw on the socket.
        self.assertNotEqual(headers["x-vigil-agent-ip"], "1.2.3.4")
        # ALLOWED_HOSTS rejects an unknown Host, so this has to be ours.
        self.assertTrue(headers["host"].startswith("localhost"))

    async def test_upstream_down_gives_502(self):
        await self.upstream.close()
        status, body = await _request("POST", str(self.relay.make_url(self.agent_path)),
                                      b"{}", {})

        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": "Vigil web service unreachable"})
        # A dead web service is the agent's problem, not the link's.
        self.assertTrue(self.recorder[-1]["connected"])

    async def test_pipe_end_to_end(self):
        url = str(self.relay.make_url("/ws/agent/session/x/"))
        async with aiohttp.ClientSession() as session, session.ws_connect(url) as agent:
            await agent.send_str("ping-1")
            self.assertEqual((await _receive(agent)).data, "ping-1")
            await agent.send_bytes(b"\x01\x02")
            self.assertEqual((await _receive(agent)).data, b"\x01\x02")

    async def test_bad_key_reports_rejection(self):
        recorder = Recorder()
        uplink = Uplink(
            Target(id="r1", url=str(self.relay.make_url("")).rstrip("/"),
                   key="wrong", key_sha256=_sha("wrong")),
            upstream=str(self.upstream.make_url("")).rstrip("/"),
            status=recorder)
        task = asyncio.create_task(uplink.run())
        self.addCleanup(_retire, task)

        # Within the first retry — a rejection observed 0.1 s later would arrive for
        # the wrong reason (a backoff ending), and the test would still be green.
        # One retry is enough: a rejection observed only after the first backoff
        # would arrive for the wrong reason and still be green.
        await _until(lambda: recorder and not recorder[-1]["connected"])
        self.assertIn("relay rejected the key", recorder[-1]["error"])


async def _receive(ws):
    return await asyncio.wait_for(ws.receive(timeout=_RECEIVE_TIMEOUT), _RECEIVE_TIMEOUT)


async def _request(method: str, url: str, data: bytes, headers: dict):
    async with aiohttp.ClientSession() as session, \
            session.request(method, url, data=data, headers=headers) as resp:
        return resp.status, await resp.json(content_type=None)


def _web_service() -> web.Application:
    """A stand-in for the Vigil web service: it reports what it was asked for."""
    app = web.Application()

    async def echo(request: web.Request) -> web.Response:
        return web.json_response({
            "method": request.method,
            "path": request.path,
            "host": request.headers.get("Host", ""),
            "body": (await request.read()).decode(),
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })

    async def pipe(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await ws.send_str(msg.data)
            elif msg.type == web.WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
        return ws

    # The real /ws/agent/session/ route is QA-56's; the link mirrors whatever
    # path the agent asked for, so an echo on that same path is enough here.
    app.router.add_get("/ws/agent/session/{name}/", pipe)
    app.router.add_route("*", "/{tail:.*}", echo)
    return app


async def _serve(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


class ForwardableTests(unittest.TestCase):
    """A compromised relay still cannot reach anything but agent paths."""

    def test_only_agent_paths_cross_the_link(self):
        from apps.relays.link import forwardable, pipeable
        self.assertTrue(forwardable("POST", "/api/v1/checkin"))
        self.assertTrue(forwardable("GET", "/api/v1/agent/wake"))
        for method, path in (("POST", "/login/"), ("GET", "/api/v1/hosts/"), ("GET", "/admin/"),
                             ("DELETE", "/api/v1/checkin"), ("GET", "@evil.example/api/v1/checkin"),
                             ("GET", "//evil.example/api/v1/checkin"),
                             ("GET", "/api/v1/agent/../../login/"), ("GET", ""),
                             ("GET", "/api/v1/agent/%2e%2e/%2e%2e/login/"),
                             ("GET", "/api/v1/agent/%2E%2E%2Flogin/"),
                             ("GET", "/api/v1/agent/..%5c..%5clogin/"),
                             ("GET", "/api/v1/agent\\..\\login/"),
                             ("GET", "/api/v1/agent/./x/"), ("GET", "/api/v1/agent//x/"),
                             ("GET", "/api/v1/checkin?next=/login/")):
            self.assertFalse(forwardable(method, path), (method, path))
        self.assertTrue(pipeable("/ws/agent/session/abc/"))
        for path in ("/ws/session/abc/", "/ws/agent/../session/x/", "@x/ws/agent/",
                     "/ws/agent/%2e%2e/session/x/", "/ws/agent/session/x/%2F"):
            self.assertFalse(pipeable(path), path)


class RefusedFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_non_agent_path_is_answered_404_without_a_request(self):
        from apps.relays.link import Uplink, Target
        sent = []
        up = Uplink(Target(id="r1", url="https://relay", key="k", key_sha256="x"),
                    upstream="http://web:8000", status=lambda *a, **k: None)

        class NoSession:
            def request(self, *a, **k):
                raise AssertionError("must not reach the web service")
        up.session = NoSession()

        async def capture(frame):
            sent.append(frame)
        up._send = capture
        await up._forward({"id": "1", "method": "POST", "path": "/login/", "query": "",
                           "headers": {}, "body": "", "ip": "1.2.3.4"})
        self.assertEqual(sent[0]["status"], 404)


class LinkOnlyCarriesAgentPathsTests(TestCase):
    def test_a_link_stamped_request_for_a_non_agent_path_is_refused(self):
        resp = self.client.post("/login/", {"username": "x", "password": "y"},
                                HTTP_X_VIGIL_LINK=link_token())
        self.assertEqual(resp.status_code, 403)
        self.assertNotEqual(self.client.get("/login/").status_code, 403)
