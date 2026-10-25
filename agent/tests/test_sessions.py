"""Live sessions on the agent (QA-57): the ticket check, the opt-in, the pumps.

Nothing here starts a real shell, ``docker``, or a real network connection:
shell sessions run an injected argv, the WebSocket is a fake object, RDP dials
a listener the test made on ``127.0.0.1:0``, and the wake loop's only door to
the network — ``WakeThread.http`` — is a fake too.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import base64
import json
import logging
import os
import pwd
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from nacl.signing import SigningKey

from vigil_agent import client, sessions
from vigil_agent.config import AgentConfig, load_config
from vigil_agent.verify import agent_fingerprint

TOKEN = "agent-token-value"
SID = "6f1e9d4c-7a3b-4c2d-9e1f-0a5b8c7d6e3f"
WAKE_URL = "https://vigil.example.com/api/v1/agent/wake"


def _config(**kwargs) -> AgentConfig:
    base = {"server_url": "https://vigil.example.com", "agent_token": TOKEN,
            "mode": "full_control"}
    base.update(kwargs)
    return AgentConfig(**base)


def _ticket(signing: SigningKey, agent_token: str = TOKEN, kind: str = "shell",
            container=None, sid: str = SID, nonce: str = "nonce-1", v: int = 1,
            exp_offset: float = 60.0) -> dict:
    """A ticket built the way the server builds one (``apps/remote/tickets.py``):
    the signature covers the canonical JSON of every field but ``sig``."""
    body = {
        "sid": sid,
        "host_id": "11111111-2222-3333-4444-555555555555",
        "agent": agent_fingerprint(agent_token),
        "kind": kind,
        "container": container,
        "exp": int(time.time() + exp_offset),
        "nonce": nonce,
        "v": v,
    }
    sig = base64.b64encode(
        signing.sign(json.dumps(body, sort_keys=True).encode()).signature
    ).decode()
    return {**body, "sig": sig}


def _frame(t: str, **kw) -> dict:
    return {"t": t, **kw}


def _in(data: bytes) -> dict:
    return _frame("in", d=base64.b64encode(data).decode())


class FakeWs:
    """The slice of a WebSocket a session uses. Every ``recv`` answers at once,
    so the frames a test lists are exactly what the session gets."""

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self):
        if self.incoming:
            return json.dumps(self.incoming.pop(0))
        return None

    def close(self) -> None:
        self.closed = True

    def frames(self, t: str) -> list[dict]:
        return [f for f in self.sent if f.get("t") == t]

    def output(self) -> bytes:
        return b"".join(base64.b64decode(f["d"]) for f in self.frames("out"))


class _ChildStopped(BaseException):
    """Stands in for ``os._exit``: a child that gives up leaves the test, and
    being a ``BaseException`` it proves the spawn guards with the same breadth
    ``os._exit`` itself would have needed."""


def _wait(predicate, timeout: float = 5.0) -> bool:
    """Poll a condition rather than sleep for it: these tests wait on threads,
    and how long that takes is the machine's business, not the test's."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _TempDir:
    def __enter__(self):
        self.path = Path(tempfile.mkdtemp())
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class CheckTicketTests(unittest.TestCase):
    def setUp(self):
        self.signing = SigningKey.generate()
        self.seen = sessions.SeenNonces()

    def _check(self, ticket, missing_key=False):
        key = None if missing_key else self.signing.verify_key
        return sessions.check_ticket(ticket, key, TOKEN, self.seen)

    def test_accepts_a_good_ticket_and_returns_its_body(self):
        body = self._check(_ticket(self.signing))
        self.assertEqual(body["sid"], SID)
        self.assertEqual(body["kind"], "shell")

    def test_tampering_with_the_kind_invalidates_the_signature(self):
        ticket = _ticket(self.signing)
        with self.assertRaises(ValueError) as ctx:
            self._check({**ticket, "kind": "container", "container": "web"})
        self.assertIn("signature", str(ctx.exception))

    def test_a_ticket_minted_for_another_agent_is_refused(self):
        ticket = _ticket(self.signing, agent_token="someone-elses-token")
        with self.assertRaises(ValueError) as ctx:
            self._check(ticket)
        self.assertIn("not for this agent", str(ctx.exception))

    def test_a_ticket_signed_for_another_key_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(SigningKey.generate()))
        self.assertIn("signature", str(ctx.exception))

    def test_a_ticket_without_a_signature_is_refused(self):
        ticket = {k: v for k, v in _ticket(self.signing).items() if k != "sig"}
        with self.assertRaises(ValueError) as ctx:
            self._check(ticket)
        self.assertIn("no signature", str(ctx.exception))

    def test_an_expired_ticket_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing, exp_offset=-1))
        self.assertIn("expired", str(ctx.exception))

    def test_a_ticket_expiring_far_in_the_future_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing, exp_offset=3600))
        self.assertIn("too far in the future", str(ctx.exception))

    def test_another_ticket_version_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing, v=2))
        self.assertIn("version", str(ctx.exception))

    def test_a_kind_this_agent_cannot_run_is_refused(self):
        # Signed as "exec": refused for what it asks for, not for its signature.
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing, kind="exec"))
        self.assertIn("unknown session kind", str(ctx.exception))

    def test_a_container_name_that_is_not_usable_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing, kind="container", container="a;b"))
        self.assertIn("container name", str(ctx.exception))

    def test_an_ordinary_container_name_is_accepted(self):
        body = self._check(_ticket(self.signing, kind="container",
                                   container="web-1.example_com"))
        self.assertEqual(body["container"], "web-1.example_com")

    def test_a_replayed_nonce_is_refused(self):
        ticket = _ticket(self.signing)
        self._check(ticket)
        with self.assertRaises(ValueError) as ctx:
            self._check(ticket)
        self.assertIn("nonce", str(ctx.exception))

    def test_a_fresh_nonce_from_the_same_server_is_accepted(self):
        self._check(_ticket(self.signing, nonce="n1"))
        self._check(_ticket(self.signing, nonce="n2"))

    def test_without_a_pinned_key_no_ticket_is_ever_enough(self):
        with self.assertRaises(ValueError) as ctx:
            self._check(_ticket(self.signing), missing_key=True)
        self.assertIn("no pinned server key", str(ctx.exception))

    def test_a_payload_that_is_not_an_object_is_refused(self):
        with self.assertRaises(TypeError):
            self._check(["not", "a", "ticket"])

    def test_nonces_are_forgotten_once_the_ticket_could_not_be_replayed(self):
        moment = time.time()
        self.assertIsNone(self.seen.use("n", until=moment + 60, now=moment))
        self.assertIn("already used", self.seen.use("n", until=moment + 60, now=moment))
        self.assertIsNone(self.seen.use("n", until=moment + 60, now=moment + 61))

    def test_an_empty_nonce_is_no_nonce_at_all(self):
        reason = self.seen.use("", until=time.time() + 60, now=time.time())
        self.assertIn("no nonce", reason)


# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------

class AllowedTests(unittest.TestCase):
    def test_monitor_mode_refuses_everything_even_with_both_flags(self):
        cfg = _config(mode="monitor", allow_remote_shell=True, allow_remote_desktop=True)
        for kind in sessions.KINDS:
            self.assertFalse(sessions.allowed(cfg, kind), kind)

    def test_the_shell_flag_opens_shells_and_container_shells_but_no_rdp(self):
        cfg = _config(mode="managed", allow_remote_shell=True)
        self.assertTrue(sessions.allowed(cfg, "shell"))
        self.assertTrue(sessions.allowed(cfg, "container"))
        self.assertFalse(sessions.allowed(cfg, "rdp"))

    def test_the_desktop_flag_opens_rdp_alone(self):
        cfg = _config(mode="managed", allow_remote_desktop=True)
        self.assertTrue(sessions.allowed(cfg, "rdp"))
        self.assertFalse(sessions.allowed(cfg, "shell"))

    def test_nothing_is_allowed_by_default(self):
        cfg = _config()
        for kind in sessions.KINDS:
            self.assertFalse(sessions.allowed(cfg, kind), kind)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_off_root_and_3389(self):
        cfg = _config()
        self.assertIs(cfg.allow_remote_shell, False)
        self.assertIs(cfg.allow_remote_desktop, False)
        self.assertEqual(cfg.remote_shell_user, "root")
        self.assertEqual(cfg.remote_desktop_port, 3389)

    def test_the_four_keys_are_read_from_yaml(self):
        with _TempDir() as tmp:
            path = tmp / "agent.yml"
            path.write_text(
                "server_url: https://vigil.example.com\n"
                "agent_token: t\n"
                "mode: managed\n"
                "allow_remote_shell: true\n"
                "allow_remote_desktop: true\n"
                "remote_shell_user: svc-shell\n"
                "remote_desktop_port: 3390\n"
            )
            cfg = load_config(path)
        self.assertIs(cfg.allow_remote_shell, True)
        self.assertIs(cfg.allow_remote_desktop, True)
        self.assertEqual(cfg.remote_shell_user, "svc-shell")
        self.assertEqual(cfg.remote_desktop_port, 3390)

    def test_keys_absent_from_yaml_mean_off(self):
        with _TempDir() as tmp, patch.object(sys, "platform", "linux"):
            path = tmp / "agent.yml"
            path.write_text("server_url: https://vigil.example.com\nagent_token: t\n")
            cfg = load_config(path)
        self.assertIs(cfg.allow_remote_shell, False)
        self.assertIs(cfg.allow_remote_desktop, False)
        self.assertEqual(cfg.remote_desktop_port, 3389)

    def test_a_mac_defaults_to_screen_sharings_port(self):
        # Screen Sharing is VNC on 5900; RDP's 3389 has nothing behind it there.
        with _TempDir() as tmp, patch.object(sys, "platform", "darwin"):
            path = tmp / "agent.yml"
            path.write_text("server_url: https://vigil.example.com\nagent_token: t\n")
            self.assertEqual(load_config(path).remote_desktop_port, 5900)
            path.write_text("server_url: https://vigil.example.com\nagent_token: t\n"
                            "remote_desktop_port: 5901\n")
            self.assertEqual(load_config(path).remote_desktop_port, 5901)

    def test_a_port_that_is_not_a_port_is_refused(self):
        for port in (0, -1, 70000):
            with self.assertRaises(ValueError, msg=port):
                _config(remote_desktop_port=port)

    def test_an_account_name_that_is_not_one_is_refused(self):
        for user in ("Root!", "with space", "", "a" * 33, "../bin"):
            with self.assertRaises(ValueError, msg=user):
                _config(remote_shell_user=user)


class CheckinPayloadTests(unittest.TestCase):
    def setUp(self):
        self.sent = {}

        class Resp:
            status_code = 200

            def raise_for_status(self_resp):
                return None

            def json(self_resp):
                return {"status": "ok", "tasks": []}

        def fake_post(url, json=None, **kwargs):
            self.sent.clear()
            self.sent.update(json or {})
            return Resp()

        patcher = patch.object(client.requests, "post", fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_keys_are_false_for_a_monitor_agent_with_the_flags_set(self):
        with _TempDir() as tmp:
            cfg = _config(mode="monitor", allow_remote_shell=True,
                          allow_remote_desktop=True, data_dir=tmp)
            client.checkin(cfg, metrics=[])
        self.assertIs(self.sent["allow_remote_shell"], False)
        self.assertIs(self.sent["allow_remote_desktop"], False)

    def test_a_host_that_opted_in_reports_the_flag_it_set(self):
        with _TempDir() as tmp:
            cfg = _config(allow_remote_shell=True, data_dir=tmp)
            client.checkin(cfg, metrics=[])
        self.assertIs(self.sent["allow_remote_shell"], True)
        self.assertIs(self.sent["allow_remote_desktop"], False)


# ---------------------------------------------------------------------------
# Wake loop
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """``WakeThread.http``: every call is recorded, and the last reply repeats
    once the list runs out — a long-poll answers again as soon as it can."""

    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.urls: list = []
        self.headers: list = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs.get("headers"))
        if self.error is not None:
            raise self.error
        if not self.replies:
            return _Resp({"sessions": []})
        return _Resp(self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


class _Flag:
    value = False

    def raise_flag(self):
        self.value = True


class _AlreadyStarted:
    """A session thread that is alive and never finishes."""

    def __init__(self, config, ticket):
        self.ticket = ticket

    def start(self):
        pass

    def is_alive(self):
        return True


class WakeLoopTests(unittest.TestCase):
    """The loop itself runs, on its own thread; only its two outside worlds —
    HTTP and the session class — are replaced."""

    def setUp(self):
        self.signing = SigningKey.generate()
        tmp = _TempDir()
        self.data_dir = tmp.__enter__()
        self.addCleanup(tmp.__exit__)
        patcher = patch.object(sessions, "SessionThread", _AlreadyStarted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _thread(self, http, config=None):
        stop = _Flag()
        key = self.signing.verify_key
        with patch.object(sessions.WakeThread, "http", http):
            thread = sessions.WakeThread(config or _config(data_dir=self.data_dir),
                                         lambda: key, lambda: stop.value)
        thread.stop_soon = stop.raise_flag
        return thread

    def _run_briefly(self, http, config=None, seconds: float = 0.3):
        thread = self._thread(http, config)
        with patch.object(sessions.WakeThread, "http", http):
            thread.start()
            time.sleep(seconds)
            thread.stop_soon()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "the wake loop outlived shutdown")
        self.assertEqual(thread.stopped, "shutdown")
        return thread

    def test_an_agent_that_did_not_opt_in_never_asks(self):
        http = _FakeHttp()
        self._run_briefly(http)
        self.assertEqual(http.urls, [], "a host that never opted in made a wake request")

    def test_one_good_ticket_opens_one_session_at_the_wake_url(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        http = _FakeHttp([{"sessions": [_ticket(self.signing)]}])
        thread = self._run_briefly(http, config)
        self.assertEqual(http.urls[0], WAKE_URL)
        self.assertEqual(http.headers[0]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(len(thread._running), 1)
        self.assertEqual(thread._running[0].ticket["sid"], SID)

    def test_a_forged_ticket_opens_nothing(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        forged = {**_ticket(self.signing), "kind": "container", "container": "web"}
        http = _FakeHttp([{"sessions": [forged]}])
        with self.assertLogs("vigil.sessions", level="WARNING") as logs:
            thread = self._run_briefly(http, config)
        self.assertEqual(thread._running, [])
        self.assertIn("remote session refused", "\n".join(logs.output))

    def test_a_ticket_for_a_kind_this_host_refused_opens_nothing(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        http = _FakeHttp([{"sessions": [_ticket(self.signing, kind="rdp")]}])
        with self.assertLogs("vigil.sessions", level="WARNING") as logs:
            thread = self._run_briefly(http, config)
        self.assertEqual(thread._running, [])
        self.assertIn("has not enabled it", "\n".join(logs.output))

    def test_the_same_ticket_in_two_wake_responses_opens_one_session(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        http = _FakeHttp([{"sessions": [_ticket(self.signing)]}])
        thread = self._thread(http, config)
        with patch.object(sessions.WakeThread, "http", http):
            thread.start()
            self.assertTrue(_wait(lambda: len(http.urls) >= 2),
                            "the loop stopped asking the server")
            thread.stop_soon()
        thread.join(3)
        self.assertGreaterEqual(len(http.urls), 2)
        self.assertEqual(len(thread._running), 1, "a replayed ticket opened a session")

    def test_a_fourth_session_is_refused_as_too_many(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        thread = self._thread(_FakeHttp(), config)
        for _ in range(sessions.MAX_SESSIONS):
            thread._running.append(_AlreadyStarted(config, {"sid": "x"}))
        with self.assertLogs("vigil.sessions", level="WARNING") as logs:
            thread._open(_ticket(self.signing, nonce="one-more"))
        self.assertEqual(len(thread._running), sessions.MAX_SESSIONS)
        self.assertIn("too many sessions", "\n".join(logs.output))

    def test_a_failing_wake_request_retries_rather_than_stopping(self):
        config = _config(allow_remote_shell=True, data_dir=self.data_dir)
        http = _FakeHttp(error=client.requests.ConnectionError("no route"))
        thread = self._thread(http, config)
        thread.retry_seconds = thread.retry_max_seconds = 0.01
        with patch.object(sessions.WakeThread, "http", http):
            thread.start()
            self.assertTrue(_wait(lambda: len(http.urls) >= 3),
                            "the loop gave up after one failed request")
            thread.stop_soon()
        thread.join(5)
        self.assertEqual(thread.stopped, "shutdown")


# ---------------------------------------------------------------------------
# Shell sessions
# ---------------------------------------------------------------------------

def _exec_only(argv, drop_user, user):
    """The child half of a test session: exec the argv, or exit."""
    try:
        os.execvp(argv[0], argv)
    finally:
        os._exit(1)


@unittest.skipUnless(os.name == "posix", "a pty session needs a POSIX host")
class ShellSessionTests(unittest.TestCase):
    """A real ``pty.fork`` running an injected argv — never a shell. The user
    drop is patched out twice over: in the test process, where the pump runs,
    and in the child the fork makes, which is the one that would call setuid."""

    def setUp(self):
        for name in ("initgroups", "setgid", "setuid"):
            patcher = patch.object(sessions.os, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The child execs the injected argv straight away: no user drop, and no
        # chance of the forked test process carrying on as a second runner.
        patcher = patch.object(sessions, "_child_setup", _exec_only)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = _config(allow_remote_shell=True)

    def _thread(self, ws, argv, kind="shell", container=None, config=None):
        return sessions.SessionThread(
            config or self.config, {"sid": SID, "kind": kind, "container": container},
            ws_factory=lambda url, token: ws, argv=argv)

    def _finish(self, ws, argv, kind="shell", container=None, timeout: float = 5.0):
        thread = self._thread(ws, argv, kind, container)
        thread.start()
        self.assertTrue(_wait(lambda: ws.closed, timeout), "session did not finish")
        self.assertTrue(_wait(lambda: thread._done.is_set(), timeout),
                        "the session thread outlived its own session")
        return thread

    def test_what_is_typed_reaches_the_child_and_comes_back(self):
        ws = FakeWs([_in(b"hello\n"), None, _frame("close")])
        self._finish(ws, ["cat"])
        # A real terminal echoes the input, then cat repeats it.
        self.assertIn(b"hello\r\n", ws.output())

    def test_the_websocket_is_opened_at_the_agent_session_url(self):
        seen: list = []
        ws = FakeWs([_frame("close")])
        config = _config(allow_remote_shell=True, server_url="https://relay/t/acme")
        thread = self._thread(ws, ["true"], config=config)
        thread.ws_factory = lambda url, token: seen.append((url, token)) or ws
        thread.start()
        self.assertTrue(_wait(lambda: ws.closed))
        self.assertEqual(seen, [(f"wss://relay/t/acme/ws/agent/session/{SID}/", TOKEN)])

    def test_the_child_is_reaped_when_the_session_ends(self):
        ws = FakeWs([_frame("close")])
        thread = self._finish(ws, ["cat"])
        with self.assertRaises(ChildProcessError):
            os.waitpid(thread._pid, os.WNOHANG)

    def test_frames_that_are_not_frames_are_ignored(self):
        ws = FakeWs(["}{ not json", _frame("bogus"), _frame("in", d="not-base64!"),
                     {"t": "in"}, _in(b"ok\n"), _frame("close")])
        self._finish(ws, ["cat"])
        self.assertIn(b"ok\r\n", ws.output())

    def test_a_resize_reaches_the_pty_and_the_session_carries_on(self):
        ws = FakeWs([_frame("resize", cols=200, rows=999), _in(b"x\n"), _frame("close")])
        self._finish(ws, ["cat"])
        self.assertIn(b"x\r\n", ws.output())

    def test_an_eof_from_the_child_ends_the_session(self):
        # ``true`` exits at once: the pump must see EOF and stop, not spin.
        ws = FakeWs([])
        thread = self._finish(ws, ["true"])
        self.assertIsNotNone(thread.reason)

    def test_a_shell_that_cannot_start_still_ends_and_cleans_up(self):
        ws = FakeWs([_frame("close")])
        self._finish(ws, ["/nonexistent/vigil-shell"])
        self.assertTrue(ws.closed)

    def test_nothing_the_user_typed_reaches_the_log(self):
        ws = FakeWs([_in(b"secret-word\n"), None, _frame("close", reason="browser left")])
        with _capture_logs() as lines:
            self._finish(ws, ["cat"])
        joined = "\n".join(lines)
        self.assertNotIn("secret-word", joined)
        self.assertIn(f"remote session {SID} shell opened", joined)
        self.assertIn("browser left", joined)

    def test_opened_and_closed_are_the_only_two_lines_a_session_writes(self):
        ws = FakeWs([_in(b"ls -l\n"), None, _frame("close")])
        with _capture_logs() as lines:
            self._finish(ws, ["cat"])
        mine = [l for l in lines if SID in l]
        self.assertEqual(len(mine), 2, "\n".join(lines))


class SpawnTests(unittest.TestCase):
    """Which argv a session would exec, and as whom. The spawn is intercepted
    where every kind's argv arrives, so no host runs a shell or an engine."""

    def _thread(self, kind="shell", container=None, user="root", argv=None):
        ws = FakeWs([_frame("close")])
        return sessions.SessionThread(
            _config(allow_remote_shell=True, remote_shell_user=user),
            {"sid": SID, "kind": kind, "container": container},
            ws_factory=lambda url, token: ws, argv=argv)

    def _captured_spawn(self, thread, kind, container=None, engine=None):
        """The argv the thread would have handed the child, without a child."""
        captured: list = []

        def fake_fork(self, argv, drop_user):
            captured.append((argv, drop_user))
            raise OSError("no child, no pump")

        def which(name):
            # Exactly one engine on PATH, as the lookup sees it: the real
            # shutil resolves the name against the PATH set below.
            return engine if name == os.path.basename(engine or "") else None

        with patch.object(sessions.SessionThread, "_pty_fork", fake_fork), \
                patch.object(sessions.shutil, "which", which), \
                self.assertRaises(OSError):
            thread._spawn({"sid": SID, "kind": kind, "container": container})
        self.assertEqual(len(captured), 1, "no argv reached the spawn")
        return captured[0]

    @unittest.skipUnless(os.name == "posix", "a shell session needs a POSIX host")
    def test_the_login_shell_of_the_configured_account_is_the_default(self):
        argv, drop_user = self._captured_spawn(self._thread(), "shell")
        self.assertEqual(argv, [pwd.getpwnam("root").pw_shell or "/bin/sh", "-l"])
        self.assertIs(drop_user, True)

    @unittest.skipUnless(os.name == "posix", "a shell session needs a POSIX host")
    def test_an_injected_argv_replaces_the_shell_rather_than_wrapping_it(self):
        argv, _drop_user = self._captured_spawn(self._thread(argv=["cat"]), "shell")
        self.assertEqual(argv, ["cat"])

    @unittest.skipUnless(os.name == "posix", "a container session needs a POSIX host")
    def test_the_container_command_is_pinned_to_the_engine(self):
        thread = self._thread(kind="container", container="web-1")
        argv, drop_user = self._captured_spawn(thread, "container", "web-1",
                                               engine="/usr/bin/docker")
        self.assertEqual(argv, [
            "/usr/bin/docker", "exec", "-it", "web-1", "sh", "-c",
            "command -v bash >/dev/null 2>&1 && exec bash || exec sh",
        ])
        # The container picks its own user; dropping here would fight it.
        self.assertIs(drop_user, False)

    @unittest.skipUnless(os.name == "posix", "a container session needs a POSIX host")
    def test_podman_stands_in_when_there_is_no_docker(self):
        thread = self._thread(kind="container", container="web-1")
        argv, _drop_user = self._captured_spawn(thread, "container", "web-1",
                                                engine="/usr/bin/podman")
        self.assertEqual(argv[0], "/usr/bin/podman")

    @unittest.skipUnless(os.name == "posix", "a container session needs a POSIX host")
    def test_no_container_engine_means_no_session(self):
        thread = self._thread(kind="container", container="web-1")
        with patch.object(sessions.shutil, "which", return_value=None), \
                self.assertRaises(OSError) as ctx:
            thread._spawn({"sid": SID, "kind": "container", "container": "web-1"})
        self.assertIn("no container engine", str(ctx.exception))

    def test_the_drop_goes_to_the_configured_account(self):
        class _Pw:
            pw_uid, pw_gid, pw_dir = 1500, 1500, "/home/svc-shell"

        asked: list = []

        def fake_getpwnam(name):
            asked.append(name)
            return _Pw()

        exited: list = []
        with patch.object(sessions.pwd, "getpwnam", fake_getpwnam), \
                patch.object(sessions.os, "geteuid", return_value=0), \
                patch.object(sessions.os, "initgroups") as initgroups, \
                patch.object(sessions.os, "setgid") as setgid, \
                patch.object(sessions.os, "setuid") as setuid, \
                patch.object(sessions.os, "execvpe") as execvpe:
            # execvpe never returns for a good shell; the fake stands in for a
            # name the account does not have, which is the case that must exit.
            execvpe.side_effect = FileNotFoundError("no such shell")
            with patch.object(sessions.os, "_exit", side_effect=_ChildStopped) as exited:
                try:
                    sessions._child_setup(["/bin/bash", "-l"], True, "svc-shell")
                except _ChildStopped:
                    pass
        self.assertEqual(asked, ["svc-shell"])
        initgroups.assert_called_once_with("svc-shell", 1500)
        setgid.assert_called_once_with(1500)
        setuid.assert_called_once_with(1500)
        self.assertEqual(exited.call_args.args, (1,),
                         "a child that cannot exec must exit, not carry on")
        execvpe.assert_called_once()
        self.assertEqual(execvpe.call_args.args[0], "/bin/bash")
        self.assertEqual(execvpe.call_args.args[1], ["/bin/bash", "-l"])
        self.assertEqual(execvpe.call_args.args[2]["PATH"],
                         "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(execvpe.call_args.args[2]["TERM"], "xterm-256color")


# ---------------------------------------------------------------------------
# RDP and cross-platform dispatch
# ---------------------------------------------------------------------------

class EchoServer:
    """An echo listener on loopback: stands in for this machine's own RDP."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _addr = self.listener.accept()
            except OSError as exc:
                logging.getLogger("vigil.tests").debug("echo listener closed: %s", exc)
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        while True:
            try:
                data = conn.recv(1024)
            except OSError as exc:
                logging.getLogger("vigil.tests").debug("echo peer went away: %s", exc)
                return
            if not data:
                return
            conn.sendall(data)

    def close(self):
        self.listener.close()


class RdpSessionTests(unittest.TestCase):
    def setUp(self):
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)

    def _thread(self, ws, port=None):
        config = _config(allow_remote_desktop=True,
                         remote_desktop_port=port if port is not None else self.echo.port)
        return sessions.SessionThread(config, {"sid": SID, "kind": "rdp", "container": None},
                                      ws_factory=lambda url, token: ws)

    def test_what_is_sent_comes_back_as_an_out_frame(self):
        ws = FakeWs([_in(b"abc")])
        thread = self._thread(ws)
        thread.start()
        self.assertTrue(_wait(lambda: ws.output() == b"abc"), "nothing echoed back")
        thread._end("test over")
        thread.join(5)

    def test_only_this_hosts_own_loopback_is_dialled(self):
        calls: list = []
        real = socket.create_connection

        def spy(address, *args, **kwargs):
            calls.append(address)
            return real(address, *args, **kwargs)

        ws = FakeWs([_frame("close")])
        thread = self._thread(ws)
        with patch.object(sessions.socket, "create_connection", side_effect=spy):
            thread.start()
            self.assertTrue(_wait(lambda: thread.reason is not None))
        thread.join(5)
        self.assertEqual(calls[0][0], "127.0.0.1")
        self.assertEqual(calls[0][1], self.echo.port)

    def test_a_dead_rdp_port_ends_the_session_with_a_reason(self):
        ws = FakeWs([_frame("close")])
        # Port 1 on loopback: nothing listens there, and nothing farther is tried.
        thread = self._thread(ws, port=1)
        thread.start()
        self.assertTrue(_wait(lambda: thread.reason is not None))
        thread.join(5)
        self.assertIn("error (", thread.reason or "")

    def test_the_rdp_socket_is_closed_when_the_session_ends(self):
        ws = FakeWs([_frame("close")])
        thread = self._thread(ws)
        thread.start()
        self.assertTrue(_wait(lambda: thread._sock is not None))
        peer = thread._sock
        thread._end("test over")
        thread.join(5)
        self.assertTrue(peer._closed)


class DispatchTests(unittest.TestCase):
    def test_a_windows_host_refuses_a_container_shell(self):
        ws = FakeWs([_frame("close")])
        config = _config(allow_remote_shell=True)
        thread = sessions.SessionThread(config, {"sid": SID, "kind": "container",
                                                 "container": "web-1"},
                                        ws_factory=lambda url, token: ws)
        with _capture_logs() as lines, patch.object(sessions.os, "name", "nt"):
            thread.start()
            thread.join(5)
        joined = "\n".join(lines)
        self.assertIn("container shells need a Linux host", joined)
        self.assertNotIn("opened", joined, "a refused session announced itself")

    def test_a_windows_shell_over_pipes_announces_a_missing_terminal(self):
        # With no pty nobody echoes what the user types: the browser has to.
        ws = FakeWs([_frame("close")])
        config = _config(allow_remote_shell=True)
        thread = sessions.SessionThread(config, {"sid": SID, "kind": "shell",
                                                 "container": None},
                                        ws_factory=lambda url, token: ws)
        with patch.object(sessions.os, "name", "nt"), \
                patch.object(sessions.subprocess, "Popen", return_value=_FakePipeProcess()):
            thread.run()   # the whole session, on this thread
        # Sent once the socket exists, before anything else.
        self.assertEqual(ws.sent[0], {"t": "mode", "pty": False})

    def test_winpty_needs_no_such_notice_because_there_is_a_terminal(self):
        ws = FakeWs([_frame("close")])
        config = _config(allow_remote_shell=True)
        thread = sessions.SessionThread(config, {"sid": SID, "kind": "shell",
                                                 "container": None},
                                        ws_factory=lambda url, token: ws)
        thread._ws = ws
        with patch.object(sessions.os, "name", "nt"), \
                patch.dict(sys.modules, {"winpty": _FakeWinpty}):
            peer = thread._spawn({"sid": SID, "kind": "shell", "container": None})
        self.assertEqual(ws.frames("mode"), [])
        self.assertEqual([peer.command], ["powershell.exe -NoLogo"])


class _FakePipeProcess:
    """The Windows pipe fallback: enough of a process for the spawn to succeed."""

    class _Stdin:
        def write(self, data):
            pass

        def flush(self):
            pass

    stdout = None
    stdin = _Stdin()

    def poll(self):
        return 0


class _FakeWinpty:
    class PtyProcess:
        @staticmethod
        def spawn(command):
            return _FakeWinptyPty(command)


class _FakeWinptyPty:
    def __init__(self, command):
        self.command = command

    def read(self, size):
        return ""


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

class UrlTests(unittest.TestCase):
    def test_a_relays_path_prefix_survives(self):
        self.assertEqual(
            sessions.session_url("https://relay.example/t/acme", SID),
            f"wss://relay.example/t/acme/ws/agent/session/{SID}/")

    def test_a_bare_host_gets_the_agent_route(self):
        self.assertEqual(
            sessions.session_url("http://127.0.0.1:8000/", SID),
            f"ws://127.0.0.1:8000/ws/agent/session/{SID}/")


def _capture_logs():
    lines: list = []

    class Handler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    class Capture:
        def __enter__(self_inner):
            self_inner.handler = Handler()
            sessions.logger.addHandler(self_inner.handler)
            self_inner.was = sessions.logger.level
            sessions.logger.setLevel(logging.DEBUG)
            return lines

        def __exit__(self_inner, *exc):
            sessions.logger.removeHandler(self_inner.handler)
            sessions.logger.setLevel(self_inner.was)
            return False

    return Capture()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Hardening added on review
# ---------------------------------------------------------------------------

class _SlowWs(FakeWs):
    """A receive that waits its full timeout when nothing is queued, as the
    real one does: what paces the pump when the browser is quiet."""

    def recv(self):
        if not self.incoming:
            time.sleep(0.2)
        return super().recv()


@unittest.skipUnless(os.name == "posix", "a pty session needs a POSIX host")
class OutputBurstTests(unittest.TestCase):
    def test_a_burst_of_output_is_not_paced_by_the_receive_timeout(self):
        # 300 KB at one 16 KiB read per 0.2 s receive would take about 4 s.
        ws = _SlowWs()
        thread = sessions.SessionThread(
            _config(allow_remote_shell=True), {"sid": SID, "kind": "shell", "container": None},
            ws_factory=lambda url, token: ws,
            argv=["sh", "-c", "head -c 300000 /dev/zero | tr '\\0' a"])
        with patch.object(sessions, "_child_setup", _exec_only):
            started = time.monotonic()
            thread.start()
            self.assertTrue(_wait(lambda: thread._done.is_set(), 10))
            elapsed = time.monotonic() - started
        self.assertEqual(ws.output().count(b"a"), 300000)
        self.assertLess(elapsed, 2.0)


class ResizeLimitTests(unittest.TestCase):
    def test_columns_go_to_500_and_rows_to_300(self):
        thread = sessions.SessionThread(_config(allow_remote_shell=True),
                                        {"sid": SID, "kind": "shell", "container": None})
        thread._fd, thread._child = 99, True
        with patch.object(sessions.fcntl, "ioctl") as ioctl:
            thread._resize(9999, 9999)
        rows, cols = struct.unpack("HHHH", ioctl.call_args.args[2])[:2]
        self.assertEqual((cols, rows), (500, 300))


class OptInIsExplicitTests(unittest.TestCase):
    def test_a_quoted_true_does_not_turn_sessions_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yml"
            path.write_text(
                "server_url: https://vigil.example.com\n"
                f"agent_token: {TOKEN}\nmode: full_control\n"
                f"data_dir: {tmp}\n"
                'allow_remote_shell: "true"\nallow_remote_desktop: "yes"\n')
            path.chmod(0o600)
            config = load_config(path)
        self.assertFalse(config.allow_remote_shell)
        self.assertFalse(config.allow_remote_desktop)


class WakeBackoffTests(unittest.TestCase):
    def _thread(self, http, flag):
        thread = sessions.WakeThread(_config(allow_remote_shell=True),
                                     lambda: None, lambda: flag.value)
        thread.http = http
        return thread

    def test_failures_back_off_and_a_good_answer_resets(self):
        flag = _Flag()
        http = _FakeHttp(error=ConnectionError("no route"))
        thread = self._thread(http, flag)
        waits: list = []

        def fake_sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 6:
                flag.raise_flag()
            return flag.value

        thread._sleep = fake_sleep
        thread.run()
        self.assertEqual(waits, [5, 10, 20, 40, 60, 60])

    def test_an_instant_empty_answer_does_not_spin(self):
        flag = _Flag()
        http = _FakeHttp(replies=[{"sessions": []}])
        thread = self._thread(http, flag)
        waits: list = []

        def fake_sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 3:
                flag.raise_flag()
            return flag.value

        thread._sleep = fake_sleep
        thread.run()
        self.assertEqual(waits, [1.0, 1.0, 1.0])
        self.assertEqual(len(http.urls), 3)
