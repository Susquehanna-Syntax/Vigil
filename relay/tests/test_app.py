"""End-to-end tests over the relay's HTTP surface, with a fake dashboard link."""

import asyncio
import base64
import hashlib
import json
import shutil
import tempfile
import unittest

from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from vigil_relay import app as relay_uplink
from vigil_relay.app import make_app
from vigil_relay.config import Config

KEY1 = "k1"
KEY2 = "k2"


def _sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _res(rid: str, status: int = 201, body: bytes = b'{"ok":1}', ctype: str = "application/json") -> dict:
    return {"t": "res", "id": rid, "status": status, "headers": {"Content-Type": ctype},
            "body": base64.b64encode(body).decode()}


class RelayAppTest(AioHTTPTestCase):
    key = KEY1
    request_timeout = 5.0

    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-test-")
        self.addAsyncCleanup(self._cleanup_tmp)
        cfg = Config(
            tenants={"default": _sha(self.key)},
            queue_dir=self.tmp,
            request_timeout=self.request_timeout,
        )
        return make_app(cfg)

    async def _cleanup_tmp(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ fake link

    async def connect_uplink(self, key: str | None = None, path: str = "/relay/uplink"):
        ws = await self.client.ws_connect(path, headers={"Authorization": f"Bearer {key or self.key}"})
        return ws

    async def answer_requests(self, ws, reply=_res, times: int = 1):
        """Fake link: for each ``req`` frame received, send back a matching ``res``."""
        seen = []
        for _ in range(times):
            msg = await ws.receive_json()
            self.assertEqual(msg["t"], "req")
            seen.append(msg)
            await ws.send_json(reply(msg["id"]))
        return seen

    async def refuse_uplink(self, key: str | None = None, path: str = "/relay/uplink") -> int:
        """A rejected uplink is refused before the upgrade. The client sees either a
        response (plain GET) or a handshake error carrying the status (real probe)."""
        headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
        try:
            resp = await self.client.request("GET", path, headers=headers)
            return resp.status
        except WSServerHandshakeError as exc:
            return exc.status

    async def probe_uplink(self, key: str | None = None, path: str = "/relay/uplink") -> int:
        """The 401 the fake link (a real WebSocket client) would see."""
        headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
        try:
            await self.client.ws_connect(path, headers=headers)
        except WSServerHandshakeError as exc:
            return exc.status
        self.fail("uplink upgraded despite a bad key")

    # ------------------------------------------------------------- health/UI

    @unittest_run_loop
    async def test_health_is_local(self) -> None:
        resp = await self.client.request("GET", "/api/v1/health/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"status": "ok", "relay": True})

    @unittest_run_loop
    async def test_ui_paths_404(self) -> None:
        for path in ("/", "/api/v1/hosts/", "/login/"):
            resp = await self.client.request("GET", path)
            self.assertEqual(resp.status, 404, path)
            self.assertEqual(await resp.json(), {"error": "not found"}, path)

    @unittest_run_loop
    async def test_bad_key_401(self) -> None:
        self.assertEqual(await self.probe_uplink("nope"), 401)
        self.assertEqual(await self.refuse_uplink("nope"), 401)

    @unittest_run_loop
    async def test_missing_authorization_401(self) -> None:
        self.assertEqual(await self.probe_uplink(), 401)

    # ----------------------------------------------------------- forwarding

    @unittest_run_loop
    async def test_forward_roundtrip(self) -> None:
        uplink = await self.connect_uplink()

        async def agent_posts():
            resp = await self.client.post(
                "/api/v1/checkin",
                json={"host": "h1"},
                headers={"Authorization": "Bearer agent-token", "X-Vigil-Link": "link-forgery",
                         "X-Forwarded-For": "203.0.113.9"},
            )
            return resp

        agent = asyncio.ensure_future(agent_posts())
        frames = await self.answer_requests(uplink)
        resp = await agent

        self.assertEqual(resp.status, 201)
        self.assertEqual(await resp.json(), {"ok": 1})
        self.assertEqual(resp.headers["Content-Type"], "application/json")

        frame = frames[0]
        self.assertEqual(frame["method"], "POST")
        self.assertEqual(frame["path"], "/api/v1/checkin")
        self.assertEqual(json.loads(base64.b64decode(frame["body"])), {"host": "h1"})
        header_names = {k.lower(): v for k, v in frame["headers"].items()}
        self.assertEqual(header_names["authorization"], "Bearer agent-token")
        self.assertNotIn("x-vigil-link", header_names)
        self.assertNotIn("x-forwarded-for", header_names)
        self.assertNotIn("host", header_names)
        self.assertNotIn("content-length", header_names)
        self.assertEqual(frame["ip"], "127.0.0.1")
        await uplink.close()


class RelayAuthTest(AioHTTPTestCase):
    key = KEY1
    request_timeout = 5.0

    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-test-")
        self.addAsyncCleanup(self._cleanup_tmp)
        cfg = Config(
            tenants={"default": _sha(self.key), "acme": _sha(KEY2)},
            queue_dir=self.tmp,
            request_timeout=self.request_timeout,
        )
        return make_app(cfg)

    async def _cleanup_tmp(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    refuse_uplink = RelayAppTest.refuse_uplink
    probe_uplink = RelayAppTest.probe_uplink

    @unittest_run_loop
    async def test_bad_key_401(self) -> None:
        self.assertEqual(await self.probe_uplink("nope"), 401)
        self.assertEqual(await self.refuse_uplink("nope"), 401)

    @unittest_run_loop
    async def test_missing_authorization_401(self) -> None:
        self.assertEqual(await self.probe_uplink(), 401)

    @unittest_run_loop
    async def test_unscoped_uplink_is_ambiguous_with_several_tenants(self) -> None:
        self.assertEqual(await self.probe_uplink(KEY2), 401)

    @unittest_run_loop
    async def test_key_selects_its_own_scoped_tenant(self) -> None:
        uplink = await self.client.ws_connect("/t/acme/relay/uplink", headers={"Authorization": f"Bearer {KEY2}"})
        self.assertFalse(uplink.closed)
        self.assertEqual(await self.probe_uplink(KEY2, "/t/default/relay/uplink"), 401)
        agent = asyncio.ensure_future(self.client.post("/t/acme/api/v1/checkin", json={}))
        frame = await asyncio.wait_for(uplink.receive_json(), 5)
        await uplink.send_json(_res(frame["id"]))
        self.assertEqual((await agent).status, 201)
        await uplink.close()


class TenantAppTest(AioHTTPTestCase):
    """A relay hosting one non-default tenant: every URL is scoped by /t/<slug>."""

    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-test-")
        self.addAsyncCleanup(self._cleanup_tmp)
        return make_app(Config(tenants={"acme": _sha(KEY2)}, queue_dir=self.tmp))

    async def _cleanup_tmp(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    refuse_uplink = RelayAppTest.refuse_uplink
    connect_uplink = RelayAppTest.connect_uplink

    @unittest_run_loop
    async def test_tenant_prefix(self) -> None:
        resp = await self.client.request("GET", "/api/v1/health/")
        self.assertEqual(resp.status, 404)
        resp = await self.client.request("GET", "/t/acme/api/v1/checkin")
        self.assertEqual(resp.status, 503)

    @unittest_run_loop
    async def test_uplink_without_a_slug_is_refused(self) -> None:
        self.assertEqual(await self.refuse_uplink(KEY2), 401)

    @unittest_run_loop
    async def test_wrong_key_for_the_slug_401(self) -> None:
        self.assertEqual(await self.refuse_uplink(KEY1, "/t/acme/relay/uplink"), 401)

    @unittest_run_loop
    async def test_tenant_scoped_health_and_uplink(self) -> None:
        resp = await self.client.request("GET", "/t/acme/api/v1/health/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"status": "ok", "relay": True})

        uplink = await self.client.ws_connect("/t/acme/relay/uplink", headers={"Authorization": f"Bearer {KEY2}"})
        self.assertFalse(uplink.closed)
        agent = asyncio.ensure_future(self.client.post("/t/acme/api/v1/checkin", json={}))
        frame = await asyncio.wait_for(uplink.receive_json(), 5)
        self.assertEqual(frame["path"], "/api/v1/checkin")
        self.assertEqual(self.client.app[relay_uplink.UPLINKS]["acme"].slug, "acme")
        await uplink.send_json(_res(frame["id"]))
        resp = await agent
        self.assertEqual(resp.status, 201)
        await uplink.close()


class TimeoutTest(RelayAppTest):
    request_timeout = 0.5

    @unittest_run_loop
    async def test_timeout_504(self) -> None:
        uplink = await self.connect_uplink()

        agent = asyncio.ensure_future(self.client.post("/api/v1/checkin", json={}))
        msg = await uplink.receive_json()
        self.assertEqual(msg["t"], "req")
        resp = await agent
        self.assertEqual(resp.status, 504)
        self.assertEqual(await resp.json(), {"error": "Vigil did not answer in time"})
        app_uplink = self.client.app[relay_uplink.UPLINKS]["default"]
        self.assertEqual(app_uplink.pending, {})
        await uplink.close()

    # -------------------------------------------------------------- offline

    @unittest_run_loop
    async def test_offline_checkin_is_queued_then_replayed(self) -> None:
        resp = await self.client.post("/api/v1/checkin", json={"host": "h1"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"status": "queued", "tasks": []})

        resp = await self.client.post("/api/v1/tasks/result/", json={"task": 1})
        self.assertEqual(resp.status, 503)
        self.assertEqual(await resp.json(), {"error": "Vigil is unreachable from this relay"})

        queue = self.client.app[relay_uplink.QUEUES]["default"]
        queued = queue.items()
        self.assertEqual(len(queued), 1)
        frame = queue.load(queued[0])
        self.assertEqual((frame["t"], frame["method"], frame["path"]), ("req", "POST", "/api/v1/checkin"))

        uplink = await self.connect_uplink()
        msg = await asyncio.wait_for(uplink.receive_json(), 5)
        self.assertEqual(msg["t"], "req")
        self.assertEqual(msg["path"], "/api/v1/checkin")
        self.assertIn("id", msg)
        await uplink.send_json(_res(msg["id"], status=200, body=b'{"tasks":[]}'))

        for _ in range(50):
            if not queue.items():
                break
            await asyncio.sleep(0.05)
        self.assertEqual(queue.items(), [])
        await uplink.close()

    @unittest_run_loop
    async def test_newest_uplink_wins(self) -> None:
        first = await self.connect_uplink()
        second = await self.connect_uplink()

        msg = await asyncio.wait_for(first.receive(timeout=5), 5)
        self.assertTrue(first.closed or msg.type == WSMsgType.CLOSE, msg)
        self.assertFalse(second.closed)

        agent = asyncio.ensure_future(self.client.post("/api/v1/checkin", json={}))
        frame = await asyncio.wait_for(second.receive_json(), 5)
        await second.send_json(_res(frame["id"]))
        resp = await agent
        self.assertEqual(resp.status, 201)
        await second.close()

    # ----------------------------------------------------------------- pipe

    @unittest_run_loop
    async def test_pipe(self) -> None:
        uplink = await self.connect_uplink()
        agent = await self.client.ws_connect("/ws/agent/session/abc/", headers={"X-Vigil-Link": "nope"})

        open_frame = await asyncio.wait_for(uplink.receive_json(), 5)
        self.assertEqual(open_frame["t"], "ws_open")
        self.assertEqual(open_frame["path"], "/ws/agent/session/abc/")
        self.assertNotIn("x-vigil-link", {k.lower() for k in open_frame["headers"]})
        pid = open_frame["id"]

        await agent.send_str("hi")
        data = await asyncio.wait_for(uplink.receive_json(), 5)
        self.assertEqual(data, {"t": "ws_data", "id": pid, "data": "hi", "bin": False})

        await uplink.send_json({"t": "ws_data", "id": pid, "data": "yo", "bin": False})
        self.assertEqual((await agent.receive()).data, "yo")

        await uplink.send_json({"t": "ws_close", "id": pid})
        for _ in range(50):
            msg = await asyncio.wait_for(agent.receive(timeout=5), 5)
            if msg.type == WSMsgType.CLOSE:
                break
        self.assertTrue(agent.closed)
        self.assertEqual(self.client.app[relay_uplink.UPLINKS]["default"].pipes, {})
        await uplink.close()

    @unittest_run_loop
    @unittest_run_loop
    async def test_pipe_without_uplink_is_503(self) -> None:
        resp = await self.client.get("/ws/agent/session/abc/",
                                     headers={"Connection": "Upgrade", "Upgrade": "websocket"})
        self.assertEqual(resp.status, 503)


if __name__ == "__main__":
    unittest.main()


class TrustProxyTest(RelayAppTest):
    """Behind the operator's own proxy the agent's address is the entry that
    proxy appended, the rightmost, never one the agent wrote itself."""

    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-test-")
        self.addAsyncCleanup(self._cleanup_tmp)
        return make_app(Config(tenants={"default": _sha(self.key)}, queue_dir=self.tmp,
                               request_timeout=self.request_timeout, trust_proxy=True))

    @unittest_run_loop
    async def test_rightmost_forwarded_for_wins(self) -> None:
        uplink = await self.connect_uplink()
        agent = asyncio.ensure_future(self.client.post(
            "/api/v1/checkin", json={},
            headers={"X-Forwarded-For": "198.51.100.66, 203.0.113.9"}))
        frames = await self.answer_requests(uplink)
        await agent
        self.assertEqual(frames[0]["ip"], "203.0.113.9")


class BusyGateTest(RelayAppTest):
    @unittest_run_loop
    async def test_full_gate_answers_busy(self) -> None:
        gate = self.app[relay_uplink.GATE]
        self.assertIsInstance(gate, asyncio.Semaphore)
        for _ in range(2000):
            await gate.acquire()
        try:
            resp = await self.client.request("GET", "/api/v1/health/")
            self.assertEqual(resp.status, 503)
            self.assertEqual(await resp.json(), {"error": "relay busy"})
        finally:
            for _ in range(2000):
                gate.release()


# The two classes above reuse RelayAppTest's fixtures; run only their own tests.
for _name in [n for n in dir(RelayAppTest) if n.startswith("test_")]:
    for _cls in (TrustProxyTest, BusyGateTest):
        if _name not in _cls.__dict__:
            setattr(_cls, _name, None)
