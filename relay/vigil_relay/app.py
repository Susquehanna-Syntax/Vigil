"""Relay HTTP surface. The relay is dumb on purpose: it knows paths, a key hash and
a queue directory, nothing else — no database, no config file, no request logging."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import time
import uuid

from aiohttp import web

from . import paths
from .config import Config
from .queue import DiskQueue
from .uplink import Uplink, UplinkDown

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
))
CLIENT_IP_HEADERS = frozenset(("x-forwarded-for", "forwarded", "x-real-ip"))

UPBEAT = 20.0
PONG_TIMEOUT = 60.0


CFG = web.AppKey("cfg", Config)
UPLINKS = web.AppKey("uplinks", dict)
QUEUES = web.AppKey("queues", dict)
GATE = web.AppKey("gate", object)
REPLAY_GATE = web.AppKey("replay_gate", object)


def make_app(cfg: Config) -> web.Application:
    app = web.Application(client_max_size=cfg.max_body, middlewares=[_gate_middleware])
    app[CFG] = cfg
    app[UPLINKS] = {slug: Uplink(slug) for slug in cfg.tenants}
    app[QUEUES] = {
        slug: DiskQueue(f"{cfg.queue_dir}/{slug}", cfg.queue_max_bytes, cfg.queue_max_items)
        for slug in cfg.tenants
    }
    app[GATE] = asyncio.Semaphore(2000)
    app[REPLAY_GATE] = asyncio.Semaphore(1)

    app.router.add_get("/relay/uplink", _uplink_handler)
    app.router.add_get("/t/{slug}/relay/uplink", _uplink_handler)
    app.router.add_route("*", "/{tail:.*}", _agent_handler)
    return app


def tenant_for_key(cfg: Config, header: str | None) -> str | None:
    """The tenant whose stored hash matches the presented key. Every tenant is
    compared; none of the comparisons short-circuit on a mismatched prefix."""
    if not header or not header.startswith("Bearer "):
        return None
    digest = hashlib.sha256(header[len("Bearer "):].encode()).hexdigest()
    match = None
    for slug, want in cfg.tenants.items():
        if hmac.compare_digest(digest, want.lower()):
            match = slug
    return match


async def _agent_handler(request: web.Request) -> web.Response | web.WebSocketResponse:
    """Everything an agent sends. Unknown paths are 404 so the dashboard UI behind the
    link is never reachable through the relay."""
    cfg: Config = request.app[CFG]
    tenant, path = paths.split_tenant(request.path, cfg.tenants)
    if tenant is None:
        return web.json_response({"error": "not found"}, status=404)
    uplink: Uplink = request.app[UPLINKS][tenant]

    if request.method in ("GET", "HEAD") and path == "/api/v1/health/":
        return web.json_response({"status": "ok", "relay": True})

    if _is_upgrade(request) and path.startswith(paths.WS_PREFIXES) and ".." not in path:
        return await _pipe(request, uplink, path)

    if not paths.allowed(request.method, path):
        return web.json_response({"error": "not found"}, status=404)

    body = await request.read()
    frame = {
        "t": "req",
        "method": request.method,
        "path": path,
        "query": request.query_string,
        "headers": _filtered_headers(request),
        "body": base64.b64encode(body).decode(),
        "ip": _client_ip(request),
    }

    if uplink.connected:
        try:
            res = await uplink.request(frame, cfg.request_timeout)
        except asyncio.TimeoutError:
            return web.json_response({"error": "Vigil did not answer in time"}, status=504)
        except UplinkDown:
            return _unreachable(request.app, tenant, frame, path)
        return _response_from(res)

    return _unreachable(request.app, tenant, frame, path)


async def _uplink_handler(request: web.Request) -> web.WebSocketResponse | web.Response:
    """The dashboard link's outbound WebSocket: one per tenant, newest wins.

    Frames from the link are dispatched to ``Uplink.handle``; malformed JSON is
    ignored rather than dropping the link. On exit every in-flight request fails and
    every agent pipe closes, so nothing waits for a link that has gone.
    """
    cfg: Config = request.app[CFG]
    keyed = tenant_for_key(cfg, request.headers.get("Authorization", ""))

    slug = request.match_info.get("slug") or ("default" if "default" in cfg.tenants else None)
    unscoped = request.match_info.get("slug") is None and slug is not None
    # The key always authenticates its own tenant; the unscoped URL may name the
    # tenant implicitly only when there is exactly one to choose from. A wrong or
    # missing key is refused before any WebSocket upgrade is attempted.
    if keyed is None or keyed != slug or (unscoped and len(cfg.tenants) > 1):
        return _bad_key(request)

    slug = keyed
    uplink = request.app[UPLINKS][slug]
    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=cfg.max_body * 2)
    await ws.prepare(request)

    previous = uplink.ws
    if previous is not None and not previous.closed:
        await previous.close()
    uplink.ws = ws
    uplink.last_pong = time.monotonic()

    ping_task = asyncio.create_task(_ping_loop(uplink, ws))
    replay_task = asyncio.create_task(_replay(request.app, uplink))
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            if isinstance(frame, dict):
                await uplink.handle(frame)
    finally:
        ping_task.cancel()
        replay_task.cancel()
        if uplink.ws is ws:
            uplink.ws = None
            uplink.fail_pending(UplinkDown(f"uplink for {slug} dropped"))
            for pipe in uplink.pipes.values():
                if not pipe.closed:
                    await pipe.close()
            uplink.pipes.clear()
    return ws


async def _pipe(request: web.Request, uplink: Uplink, path: str) -> web.WebSocketResponse:
    """Bridge one agent WebSocket to the uplink. Frames are tagged with a relay-side
    pipe id; the link's ``ws_data``/``ws_close`` finds the agent socket by it."""
    if not uplink.connected:
        return web.json_response({"error": "Vigil is unreachable from this relay"}, status=503)

    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=request.app[CFG].max_body * 2)
    await ws.prepare(request)

    pid = uuid.uuid4().hex
    uplink.pipes[pid] = ws
    try:
        await uplink.send({
            "t": "ws_open",
            "id": pid,
            "path": path,
            "query": request.query_string,
            "headers": _filtered_headers(request),
            "ip": _client_ip(request),
        })
    except UplinkDown:
        uplink.pipes.pop(pid, None)
        await ws.close()
        return ws

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await uplink.send({"t": "ws_data", "id": pid, "data": msg.data, "bin": False})
            elif msg.type == web.WSMsgType.BINARY:
                await uplink.send({
                    "t": "ws_data", "id": pid,
                    "data": base64.b64encode(msg.data).decode(), "bin": True,
                })
            else:
                break
    finally:
        uplink.pipes.pop(pid, None)
        if uplink.connected:
            try:
                await uplink.send({"t": "ws_close", "id": pid})
            except UplinkDown:
                pass
    return ws


async def _ping_loop(uplink: Uplink, ws: web.WebSocketResponse) -> None:
    try:
        while not ws.closed:
            await asyncio.sleep(UPBEAT)
            if time.monotonic() - uplink.last_pong > PONG_TIMEOUT:
                await ws.close()
                return
            await ws.send_json({"t": "ping"})
    except (asyncio.CancelledError, ConnectionResetError):
        return


async def _replay(app: web.Application, uplink: Uplink) -> None:
    """Drain this tenant's offline buffer, oldest first, once the uplink is up.
    Only one tenant replays at a time; anything after the first failure stays queued."""
    cfg: Config = app[CFG]
    queue = app[QUEUES][uplink.slug]
    async with app[REPLAY_GATE]:
        while True:
            queued = queue.items()
            if not queued:
                return
            path = queued[0]
            try:
                frame = queue.load(path)
            except (OSError, ValueError):
                queue.drop(path)
                continue
            try:
                await uplink.request(frame, cfg.request_timeout)
            except (UplinkDown, asyncio.TimeoutError):
                return
            queue.drop(path)


def _unreachable(app: web.Application, tenant: str, frame: dict, path: str) -> web.Response:
    """An agent's checkin is the one request worth buffering: it carries no answer the
    agent needs right now, and replaying it is what makes an offline host show up."""
    buffered = frame["method"] == "POST" and path == "/api/v1/checkin"
    if buffered and app[QUEUES][tenant].put(frame):
        return web.json_response({"status": "queued", "tasks": []})
    return web.json_response({"error": "Vigil is unreachable from this relay"}, status=503)


def _response_from(res: dict) -> web.Response:
    raw = res.get("body") or ""
    try:
        body = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
    except (ValueError, TypeError):
        body = b""
    headers = {}
    for name, value in (res.get("headers") or {}).items():
        if name.lower() in HOP_BY_HOP:
            continue
        headers[name] = str(value)
    return web.Response(status=int(res.get("status", 502)), body=body, headers=headers)


def _filtered_headers(request: web.Request) -> dict[str, str]:
    """Headers to forward. ``X-Vigil-*`` is the dashboard's own attestation space — a
    relay must not be able to inject it — and client-IP headers are replaced by ``ip``."""
    out = {}
    for name, value in request.headers.items():
        low = name.lower()
        if low in HOP_BY_HOP or low.startswith("x-vigil-") or low in CLIENT_IP_HEADERS:
            continue
        out[name] = value
    return out


def _client_ip(request: web.Request) -> str:
    cfg: Config = request.app[CFG]
    if cfg.trust_proxy:
        # The rightmost entry is the one the proxy in front of us appended; every
        # entry left of it came from the client and proves nothing.
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if parts:
            try:
                return str(ipaddress.ip_address(parts[-1]))
            except ValueError:
                pass
    return request.remote or ""


def _is_upgrade(request: web.Request) -> bool:
    if not request.headers.get("Upgrade", "").lower().startswith("websocket"):
        return False
    return "upgrade" in request.headers.get("Connection", "").lower()


def _bad_key(request: web.Request) -> web.Response:
    """401 ahead of the upgrade. A real upgrade probe gets a bare 401 (the client's
    handshake error carries the status); anything else sees the JSON error."""
    if _is_upgrade(request):
        return web.Response(status=401)
    return web.json_response({"error": "bad relay key"}, status=401)


@web.middleware
async def _gate_middleware(request: web.Request, handler) -> web.Response:
    if request.path.endswith("/relay/uplink"):
        return await handler(request)
    gate: asyncio.Semaphore = request.app[GATE]
    if gate.locked():
        return web.json_response({"error": "relay busy"}, status=503)
    async with gate:
        return await handler(request)
