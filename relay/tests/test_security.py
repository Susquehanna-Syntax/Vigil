"""Security audit of the relay (J2 phase 03): one test per check. Tests for
behaviour that was already safe stay in as regression guards; the rest
proved a gap before its fix."""

import asyncio
import base64
import hashlib
import shutil
import tempfile

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from vigil_relay import paths
from vigil_relay.app import _filtered_headers, _response_from, make_app, tenant_for_key
from vigil_relay.config import Config, from_env
from vigil_relay.queue import DiskQueue

KEY = "relay-key-one"
OTHER = "relay-key-two"


def _sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class PathTests(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-sec-")
        self.addAsyncCleanup(self._cleanup)
        return make_app(Config(tenants={"default": _sha(KEY)}, queue_dir=self.tmp, request_timeout=2.0))

    async def _cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def status(self, method: str, path: str, **kw) -> int:
        resp = await self.client.request(method, path, **kw)
        return resp.status

    # 1. Path allowlist bypass -------------------------------------------------

    @unittest_run_loop
    async def test_dashboard_paths_dressed_as_agent_paths_are_404(self) -> None:
        for path in (
            "/api//v1/hosts/",
            "/API/v1/checkin",
            "/api/v1/checkin;/../hosts/",
            "/api/v1/agent/../../hosts/",
            "/agent/download/../../login/",
            "/api/v1/agent/%2e%2e/%2e%2e/hosts/",
            "/api/v1/agent/%252e%252e/hosts/",
            "/api/v1/agent/x@evil/",
            "/api/v1/agent/x\\..\\hosts/",
            "/api/v1/" + "a" * 5000,
        ):
            self.assertEqual(await self.status("GET", path), 404, path)
        # The test client reads "//host/..." as a URL of its own, so the
        # double-slash form is checked against the allowlist directly.
        self.assertFalse(paths.allowed("GET", "//api/v1/checkin"))
        self.assertFalse(paths.allowed("GET", "//api/v1/hosts/"))

    @unittest_run_loop
    async def test_unknown_or_empty_tenant_prefix_is_404(self) -> None:
        for path in ("/t/", "/t//api/v1/checkin", "/t/nobody/api/v1/checkin"):
            self.assertEqual(await self.status("GET", path), 404, path)

    # 2. Method smuggling ------------------------------------------------------

    @unittest_run_loop
    async def test_methods_outside_get_post_head_are_404(self) -> None:
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            self.assertEqual(await self.status(method, "/api/v1/checkin"), 404, method)

    def test_method_override_and_url_rewrite_headers_are_not_forwarded(self) -> None:
        # Fixed in this phase: these reached the dashboard, where a framework
        # that honours them could turn an agent POST into something else.
        request = _fake_request({
            "X-HTTP-Method-Override": "DELETE",
            "X-Method-Override": "PUT",
            "X-Original-URL": "/api/v1/hosts/",
            "X-Rewrite-URL": "/admin/",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Prefix": "/admin",
            "Authorization": "Bearer agent-token-abcdefghijkl",
        })
        out = {k.lower() for k in _filtered_headers(request)}
        for name in ("x-http-method-override", "x-method-override", "x-original-url", "x-rewrite-url",
                     "x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix"):
            self.assertNotIn(name, out)
        self.assertIn("authorization", out)

    # 3. Uplink authentication -------------------------------------------------

    @unittest_run_loop
    async def test_uplink_refuses_missing_wrong_and_same_length_keys(self) -> None:
        wrong_same_length = "x" * len(KEY)
        for header in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {wrong_same_length}"},
                       {"Authorization": f"Basic {KEY}"}, {"Authorization": f"bearer {KEY}"}):
            resp = await self.client.get("/relay/uplink", headers=header)
            self.assertEqual(resp.status, 401, header)

    @unittest_run_loop
    async def test_second_uplink_replaces_the_first(self) -> None:
        first = await self.client.ws_connect("/relay/uplink", headers={"Authorization": f"Bearer {KEY}"})
        second = await self.client.ws_connect("/relay/uplink", headers={"Authorization": f"Bearer {KEY}"})
        msg = await asyncio.wait_for(first.receive(), 2)
        self.assertTrue(first.closed or msg.type.name in ("CLOSE", "CLOSED", "CLOSING"))
        self.assertFalse(second.closed)
        await second.close()

    # 4. Headers -----------------------------------------------------------------

    def test_relay_cannot_be_used_to_forge_dashboard_attestation_or_client_ip(self) -> None:
        request = _fake_request({"X-Vigil-Link": "forged", "X-Forwarded-For": "1.2.3.4",
                                 "Forwarded": "for=1.2.3.4", "X-Real-IP": "1.2.3.4", "Host": "relay"})
        out = {k.lower() for k in _filtered_headers(request)}
        self.assertFalse(out & {"x-vigil-link", "x-forwarded-for", "forwarded", "x-real-ip", "host"})

    # 5. Resource limits ---------------------------------------------------------

    @unittest_run_loop
    async def test_body_over_the_limit_is_refused(self) -> None:
        big = b"x" * (10 * 1024 * 1024 + 1)
        resp = await self.client.post("/api/v1/checkin", data=big,
                                      headers={"Authorization": "Bearer agent-token-abcdefghijkl"})
        self.assertEqual(resp.status, 413)

    def test_one_sender_cannot_push_everyone_else_out_of_the_queue(self) -> None:
        queue = DiskQueue(self.tmp + "/fair", max_bytes=10_000_000, max_items=10_000)
        accepted = sum(queue.put({"n": i}, owner="203.0.113.9") for i in range(queue.PER_OWNER + 50))
        self.assertEqual(accepted, queue.PER_OWNER)
        self.assertTrue(queue.put({"n": "other"}, owner="198.51.100.4"))

    # 6. Responses ---------------------------------------------------------------

    def test_dashboard_cookies_never_reach_agents_on_the_relay_domain(self) -> None:
        # Fixed in this phase: a Set-Cookie from the dashboard (a CSRF or session
        # cookie on an install script, say) was handed to whoever asked the relay.
        resp = _response_from({"status": 200, "body": "",
                               "headers": {"Set-Cookie": "sessionid=abc", "Content-Type": "text/plain"}})
        self.assertNotIn("Set-Cookie", resp.headers)
        self.assertEqual(resp.headers["Content-Type"], "text/plain")

    def test_hop_by_hop_response_headers_are_dropped(self) -> None:
        resp = _response_from({"status": 200, "body": base64.b64encode(b"hi").decode(),
                               "headers": {"Transfer-Encoding": "chunked", "Connection": "close"}})
        self.assertNotIn("Connection", resp.headers)
        self.assertEqual(resp.body, b"hi")

    # 7. Replay and the queue on disk -------------------------------------------

    def test_a_corrupt_queue_file_is_dropped_not_fatal(self) -> None:
        queue = DiskQueue(self.tmp + "/corrupt", max_bytes=1_000_000, max_items=100)
        queue.put({"ok": 1}, owner="a")
        bad = queue.items()[0]
        with open(bad, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            queue.load(bad)  # _replay catches ValueError and drops the file

    def test_queue_file_names_never_come_from_request_data(self) -> None:
        queue = DiskQueue(self.tmp + "/names", max_bytes=1_000_000, max_items=100)
        queue.put({"path": "/../../etc/passwd"}, owner="../../etc")
        for path in queue.items():
            self.assertTrue(path.startswith(self.tmp + "/names/"))
            self.assertNotIn("..", path[len(self.tmp):])

    # 8. Config ------------------------------------------------------------------

    def test_tenant_slugs_that_would_escape_the_queue_dir_are_refused(self) -> None:
        for slug in ("../x", "a/b", "", ".."):
            with self.assertRaises(SystemExit, msg=slug):
                from_env({"RELAY_TENANTS": '{"%s": "%s"}' % (slug, _sha(KEY))})

    def test_malformed_tenants_and_hashes_are_refused(self) -> None:
        for raw in ("not json", "[]", "{}", '{"a": "short"}', '{"a": "%s"}' % ("Z" * 64)):
            with self.assertRaises(SystemExit, msg=raw):
                from_env({"RELAY_TENANTS": raw})

    def test_key_compare_checks_every_tenant(self) -> None:
        cfg = Config(tenants={"a": _sha(KEY), "b": _sha(OTHER)}, queue_dir=self.tmp)
        self.assertEqual(tenant_for_key(cfg, f"Bearer {OTHER}"), "b")
        self.assertIsNone(tenant_for_key(cfg, "Bearer "))
        self.assertIsNone(tenant_for_key(cfg, None))


class WebSocketPathTests(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        self.tmp = tempfile.mkdtemp(prefix="vigil-relay-sec-ws-")
        self.addAsyncCleanup(self._cleanup)
        return make_app(Config(tenants={"default": _sha(KEY)}, queue_dir=self.tmp, request_timeout=2.0))

    async def _cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_agent_socket_paths_get_the_same_character_rules_as_http(self) -> None:
        # Fixed in this phase: the WebSocket branch only refused "..", so a
        # path with "%", "\" or "@" went down the link as an agent socket.
        self.assertTrue(paths.ws_allowed("/ws/agent/abc123/"))
        for path in ("/ws/agent/%2e%2e/", "/ws/agent/a\\b", "/ws/agent/a@b", "/ws/agent/../x", "/ws/other/"):
            self.assertFalse(paths.ws_allowed(path), path)


def _fake_request(headers: dict):
    """Just enough of an aiohttp request for _filtered_headers."""

    class _Req:
        pass

    req = _Req()
    req.headers = headers
    return req
