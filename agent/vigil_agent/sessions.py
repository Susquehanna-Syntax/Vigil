"""Live sessions the host itself opted into (QA-57).

The server decides that a session exists; only this machine's ``agent.yml``
decides whether it will ever run one. So every ticket is checked twice: once
cryptographically (the same Ed25519 key the agent verifies v2 task signatures
with, bound to this agent's token fingerprint — see the reference
``server/apps/remote/tickets.py``) and once against the local config. A ticket
whose kind the host has not enabled is refused here, and nothing else happens.

An agent that has not opted in changes nothing at all: it makes no wake request,
so a server cannot even learn it exists by watching for polls.
"""

import base64
import binascii
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from nacl.exceptions import BadSignatureError

from .verify import agent_fingerprint

logger = logging.getLogger("vigil.sessions")

if os.name == "posix":
    import fcntl
    import pty
    import pwd
    import select
    import signal
    import struct
    import termios

#: A session never outlives this, however long both peers stay quiet. Matches
#: the server's own hard limit (apps/remote/ws.py).
MAX_SECONDS = 8 * 3600
KINDS = ("shell", "container", "rdp")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_WAKE_PATH = "/api/v1/agent/wake"
_SESSION_PATH = "/ws/agent/session/{sid}/"
#: The server holds a wake request up to 25 s; the read timeout must outlast it
#: or every idle poll looks like an outage.
_WAKE_TIMEOUT = (10, 35)
_IDLE_POLL_SECONDS = 60
#: After a failed wake request: 5 s, doubling to at most 60 s. A good answer
#: resets it. The first retry lands well inside a ticket's 60 s life.
_RETRY_SECONDS = 5
_RETRY_MAX_SECONDS = 60
#: An empty answer faster than this (an old server, a relay with no link) is
#: not a long-poll, and asking again at once would spin.
_MIN_POLL_SECONDS = 1.0
#: A ticket is minted seconds before it is read and lives 60 s, so one claiming
#: to expire two minutes out was not minted by the server it came from.
_MAX_AHEAD_SECONDS = 120
#: What a refusal looks like to the server: the session never opens. There is
#: deliberately no channel for a refusal to travel back on.
MAX_SESSIONS = 3
_READ_BYTES = 16 * 1024
_PIPE_READ = 4096
_POLL_SECONDS = 0.2
_TERM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_LOGIN_SHELL_FALLBACK = "/bin/sh"
#: ``-Command -`` reads the commands it runs from stdin, so a PowerShell pipe
#: behaves like the shell the browser is presenting.
_WINDOWS_SHELL_ARGV = ["powershell.exe", "-NoLogo", "-NoExit", "-Command", "-"]


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class SeenNonces:
    """Replay guard for tickets: an in-memory nonce store with expiry.

    A ticket lives 60 s, so remembering nonces while any valid one could still
    arrive is enough — nothing here has to survive a restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def use(self, nonce, until: float, now: float) -> str | None:
        """The reason to refuse this ticket's nonce, or None to accept and
        record it."""
        with self._lock:
            self._seen = {n: u for n, u in self._seen.items() if u > now}
            if not isinstance(nonce, str) or not nonce:
                return "ticket has no nonce"
            if nonce in self._seen:
                return "ticket nonce was already used"
            self._seen[nonce] = max(until, now)
            return None


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True).encode()


def _check_body(body: dict, moment: float) -> str | None:
    """Why the ticket's own fields disqualify it, or None. Everything here is
    independent of the signature, but it is only ever asked about a body whose
    signature already verified — otherwise an attacker could pick which reason
    an agent logs."""
    exp = body.get("exp")
    if exp is None or isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return "ticket has no usable expiry"
    exp = int(exp)
    if exp < moment:
        return "ticket has expired"
    if exp > moment + _MAX_AHEAD_SECONDS:
        return "ticket expires too far in the future"

    kind = body.get("kind")
    if kind not in KINDS:
        return f"unknown session kind {kind!r}"
    if kind == "container" and not _CONTAINER_RE.match(str(body.get("container") or "")):
        return "container name is not usable"
    return None


def check_ticket(ticket, verify_key, agent_token: str, seen: SeenNonces,
                 now: float | None = None) -> dict:
    """The check this agent performs on a wake ticket. Raises ``ValueError``
    (or ``TypeError`` for a payload that is not an object at all) with a short
    reason for anything it should refuse; returns the signed body otherwise.
    Ported from the server's reference ``verify_ticket``, plus what only a
    receiver can check: who the ticket is for, and whether its nonce has been
    used."""
    if verify_key is None:
        raise ValueError("no pinned server key")
    if not isinstance(ticket, dict):
        raise TypeError("ticket is not an object")
    moment = time.time() if now is None else float(now)

    sig = ticket.get("sig")
    if not isinstance(sig, str) or not sig:
        raise ValueError("ticket has no signature")
    body = {k: v for k, v in ticket.items() if k != "sig"}
    if body.get("v") != 1:
        raise ValueError(f"unsupported ticket version {body.get('v')!r}")

    try:
        verify_key.verify(_canonical(body), base64.b64decode(sig))
    except (BadSignatureError, binascii.Error, ValueError, TypeError) as exc:
        raise ValueError("ticket signature does not verify") from exc

    if body.get("agent") != agent_fingerprint(agent_token):
        raise ValueError("ticket is not for this agent")

    reason = _check_body(body, moment)
    if reason:
        raise ValueError(reason)
    if not isinstance(body.get("sid"), str) or not body["sid"]:
        raise ValueError("ticket has no session id")

    nonce_reason = seen.use(body.get("nonce"), float(int(body["exp"])), moment)
    if nonce_reason:
        raise ValueError(nonce_reason)
    return body


def allowed(config, kind: str) -> bool:
    """Whether this host's own config permits a session of *kind*."""
    if kind == "rdp":
        return bool(config.remote_desktop_allowed)
    return bool(config.remote_shell_allowed)


# ---------------------------------------------------------------------------
# Wake loop
# ---------------------------------------------------------------------------

class WakeThread(threading.Thread):
    """The long-poll that learns a session exists. Daemon: an agent shutting
    down must not be held open by a socket the server is still holding."""

    #: Patched out in tests: this thread's only door to the network, so nothing
    #: else about it — the ticket check, the opt-in, the session threads — has
    #: to be patched at all.
    http = requests

    def __init__(self, config, get_verify_key, is_shutdown):
        super().__init__(daemon=True, name="vigil-wake")
        self.config = config
        self.get_verify_key = get_verify_key
        self.is_shutdown = is_shutdown
        self.seen = SeenNonces()
        #: Why the loop stopped, so a caller (or a test) can tell a shutdown
        #: that was asked for from a thread that fell over.
        self.stopped: str | None = None
        self._running: list = []
        #: Patchable in tests, like the sleeps they pace.
        self.retry_seconds = _RETRY_SECONDS
        self.retry_max_seconds = _RETRY_MAX_SECONDS
        self.min_poll_seconds = _MIN_POLL_SECONDS
        self.idle_poll_seconds = _IDLE_POLL_SECONDS

    def run(self) -> None:
        self._loop()
        self.stopped = "shutdown"

    def _loop(self) -> None:
        backoff = self.retry_seconds
        while not self.is_shutdown():
            if not (self.config.remote_shell_allowed
                    or self.config.remote_desktop_allowed):
                # Not opted in: no request at all, so no evidence this agent
                # exists for anyone watching the server.
                if self._sleep(self.idle_poll_seconds):
                    return
                continue
            started = time.monotonic()
            try:
                opened = self._poll_once()
            except Exception as exc:  # noqa: BLE001 — this thread must not die
                # Whatever broke, sessions stay available: log it, wait, retry.
                logger.warning("wake poll failed: %s", exc)
                if self._sleep(backoff):
                    return
                backoff = min(backoff * 2, self.retry_max_seconds)
                continue
            backoff = self.retry_seconds
            quick = time.monotonic() - started < self.min_poll_seconds
            if quick and not opened and self._sleep(self.min_poll_seconds):
                return

    def _poll_once(self) -> int:
        """One long-poll; how many tickets it carried."""
        url = self.config.server_url.rstrip("/") + _WAKE_PATH
        resp = self.http.get(url, timeout=_WAKE_TIMEOUT,
                             headers={"Authorization": f"Bearer {self.config.agent_token}"})
        resp.raise_for_status()
        body = resp.json()
        tickets = body.get("sessions") if isinstance(body, dict) else None
        if not isinstance(tickets, list):
            return 0
        for ticket in tickets:
            self._open(ticket)
        return len(tickets)

    def _open(self, ticket) -> None:
        kind = ticket.get("kind") if isinstance(ticket, dict) else "?"
        try:
            body = check_ticket(ticket, self.get_verify_key(),
                                self.config.agent_token, self.seen)
        except (ValueError, TypeError) as exc:
            _refuse(kind, str(exc))
            return
        if not allowed(self.config, kind):
            _refuse(kind, "this host has not enabled it")
            return
        # Drop finished sessions: this list is how the agent holds a shell open,
        # and it must not hold one open after the shell has gone.
        self._running = [t for t in self._running if t.is_alive()]
        if len(self._running) >= MAX_SESSIONS:
            _refuse(kind, "too many sessions")
            return
        thread = SessionThread(self.config, body)
        self._running.append(thread)
        thread.start()

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep; True when shutdown cut it short."""
        deadline = time.monotonic() + seconds
        while True:
            if self.is_shutdown():
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(1.0, left))


def start(config, get_verify_key, is_shutdown) -> WakeThread:
    thread = WakeThread(config, get_verify_key, is_shutdown)
    thread.start()
    return thread


def _refuse(kind, reason: str) -> None:
    logger.warning("remote session refused: %s (%s)", reason, kind)


# ---------------------------------------------------------------------------
# The WebSocket
# ---------------------------------------------------------------------------

class Ws:
    """``websocket-client``'s connection with a timeout turned into ``None``.

    The pump polls the local end and the socket in turn, so an idle socket is
    ordinary — a value, not an error to be raised about."""

    def __init__(self, conn):
        self._conn = conn

    def send(self, text: str) -> None:
        self._conn.send(text)

    def recv(self):
        import websocket

        try:
            return self._conn.recv()
        except websocket.WebSocketTimeoutException:
            return None

    def close(self) -> None:
        # The peer left in every case that gets here, and the session is over
        # either way — but say what we tried.
        try:
            self._conn.close()
        except OSError as exc:
            logger.debug("session websocket close failed: %s", exc)


def _connect(url: str, token: str):
    """The real WebSocket. The agent authenticates with its own token: the
    server will not accept a session socket for a ticket it did not issue."""
    import websocket

    conn = websocket.create_connection(
        url,
        header=[f"Authorization: Bearer {token}"],
        sslopt={"ca_certs": requests.certs.where()},
        timeout=_POLL_SECONDS * 10,
    )
    conn.settimeout(_POLL_SECONDS)
    return Ws(conn)


def session_url(server_url: str, sid: str) -> str:
    """The agent socket for *sid*, keeping a relay's path prefix."""
    parts = urlsplit(server_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc,
                       parts.path.rstrip("/") + _SESSION_PATH.format(sid=sid),
                       "", ""))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class SessionThread(threading.Thread):
    """One live session: the peer end of a pty, a process, or a socket.

    The ticket arriving here has already had its signature checked against the
    pinned server key and its nonce spent by :class:`WakeThread` — that is what
    makes it safe to hand this class a payload that came off the network."""

    def __init__(self, config, ticket, ws_factory=_connect, argv=None):
        super().__init__(daemon=True, name=f"vigil-session-{ticket.get('sid')}")
        self.config = config
        self.ticket = ticket
        self.ws_factory = ws_factory
        self.argv = list(argv) if argv else None
        self.sid = str(ticket["sid"])
        self.kind = ticket["kind"]
        #: What went to the local end — an engine's exec command, or an account's
        #: own login shell. Nothing here assembles a command out of a ticket,
        #: so this is the whole of what a session can run.
        self.spawned_argv: list | None = None
        self._ws = None
        self._fd: int | None = None
        self._pid: int | None = None
        self._child = False
        self._proc = None
        self._winpty = None
        self._sock: socket.socket | None = None
        self._reader: _OutputReader | None = None
        self._done = threading.Event()
        #: Every blocking wait uses this. It is what lets one loop watch a
        #: local fd and a WebSocket without either being able to hang the
        #: session: nothing here waits on the network.
        self.poll_seconds = _POLL_SECONDS
        self._stopping = threading.Event()
        self.reason: str | None = None

    def run(self) -> None:
        try:
            if os.name != "posix" and self.kind == "container":
                # No docker on Windows, and no pty to run it in if there were.
                raise OSError("container shells need a Linux host")
            # The local end opens first, the socket second: a WebSocket is what
            # the server counts as "the agent answered", so connecting one for
            # a shell that cannot start here would spend a session on nothing.
            peer = self._spawn(self.ticket)
            ws = self.ws_factory(session_url(self.config.server_url, self.sid),
                                 self.config.agent_token)
            self._ws = ws
            if self._proc is not None:
                # A shell on pipes has no terminal to echo what is typed: the
                # browser has to, or the session looks dead.
                ws.send(json.dumps({"t": "mode", "pty": False}))
            logger.info("remote session %s %s opened", self.sid, self.kind)
            self._pump(ws, peer)
        except (OSError, ValueError) as exc:
            # A session that cannot start and a session whose peer died look
            # the same from here: it ends, with the reason in one log line.
            logger.debug("session %s ended: %s", self.sid, exc)
            self._end(f"error ({type(exc).__name__})")
        finally:
            # The pump can only be waiting on this thread's own sockets, but
            # say so anyway: a thread that outlives a shutdown holds a shell.
            self._cleanup()
            logger.info("remote session %s closed (%s)", self.sid,
                        self.reason or "closed")
            self._done.set()

    # -- the local end -------------------------------------------------------

    def _spawn(self, ticket: dict):
        if ticket["kind"] == "rdp":
            # Never any other address: this host's own Remote Desktop.
            self._sock = socket.create_connection(
                ("127.0.0.1", int(self.config.remote_desktop_port)), timeout=5)
            return self._sock
        if os.name != "posix":
            return self._spawn_windows()
        if ticket["kind"] == "container":
            # The container decides its own user; this agent only starts it.
            return self._pty_fork(self.argv or self._container_argv(ticket),
                                  drop_user=False)
        return self._pty_fork(self.argv or _login_shell_argv(self.config), drop_user=True)

    def _container_argv(self, ticket: dict) -> list:
        engine = shutil.which("docker") or shutil.which("podman")
        if not engine:
            raise OSError("no container engine on this host")
        container = str(ticket.get("container") or "")
        if not _CONTAINER_RE.match(container):
            raise ValueError("container name is not usable")
        return [engine, "exec", "-it", container, "sh", "-c",
                "command -v bash >/dev/null 2>&1 && exec bash || exec sh"]

    def _pty_fork(self, argv: list, drop_user: bool):
        self.spawned_argv = argv
        pid, fd = pty.fork()
        if pid == 0:
            # Execs or exits; the finally makes sure the child can never fall
            # back into this agent's code, whatever happens in between.
            try:
                _child_setup(argv, drop_user, self.config.remote_shell_user)
            finally:
                os._exit(1)
        self._pid = pid
        self._fd = fd
        self._child = True
        return fd

    def _spawn_windows(self):
        try:
            import winpty  # type: ignore
        except ImportError as exc:
            logger.debug("winpty unavailable, falling back to pipes: %s", exc)
        else:
            self._winpty = winpty.PtyProcess.spawn("powershell.exe -NoLogo")
            return self._winpty
        self._proc = subprocess.Popen(
            _WINDOWS_SHELL_ARGV,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        return self._proc

    # -- the pump ------------------------------------------------------------

    def _pump(self, ws, peer) -> None:
        deadline = time.monotonic() + MAX_SECONDS
        if isinstance(peer, socket.socket):
            # RDP is a socket on every host and no host can select() one the
            # way it selects a pty, so it reads on a thread of its own. Frames
            # keep being read here, on this thread.
            self._start_reader(ws, peer)
            self._pump_frames(ws, deadline, self._reader)
            return
        if os.name != "posix":
            # No select() for a process' pipes either: same shape as above.
            self._start_reader(ws, None)
            self._pump_frames(ws, deadline, self._reader)
            return
        self._pump_select(ws, peer, deadline)

    def _start_reader(self, ws, peer) -> None:
        # A socket reader blocks in recv() until the peer goes away. That wait
        # belongs to the peer, so it gets its own event: closing it from here
        # would drop a live RDP connection the moment the pump decided to stop.
        self._reader = _OutputReader(ws, self.poll_seconds,
                                     peer_stopped=threading.Event()
                                     if isinstance(peer, socket.socket) else None)
        self._reader.bind(self._winpty, self._proc, peer)
        self._reader.start()

    def _pump_frames(self, ws, deadline: float, reader) -> None:
        while not self._stopping.is_set():
            if time.monotonic() >= deadline:
                self._end("time limit")
                return
            raw = ws.recv()
            if raw is None:
                continue
            if not self._handle_frame(raw):
                return
            if reader.exited:
                self._end("closed")
                return

    def _pump_select(self, ws, peer, deadline: float) -> None:
        while not self._stopping.is_set():
            if time.monotonic() >= deadline:
                self._end("time limit")
                return
            # Short: ws.recv() below is what paces an idle loop.
            readable, _, exceptional = select.select([peer], [], [peer], 0.02)
            if exceptional:
                self._end("closed")
                return
            # Forward everything ready (up to 1 MiB) before looking at the
            # socket again, so a burst of output is not paced at one read per
            # receive timeout.
            for _ in range(64):
                if not readable:
                    break
                data = self._read(peer)
                if not data:
                    self._end("closed")
                    return
                ws.send(json.dumps({"t": "out", "d": base64.b64encode(data).decode()}))
                readable, _, _ = select.select([peer], [], [], 0)
            raw = ws.recv()
            if raw is None:
                continue
            if not self._handle_frame(raw):
                return

    def _read(self, peer) -> bytes:
        # EOF and a dead fd are the same news here: nothing more will arrive.
        try:
            return os.read(peer, _READ_BYTES)
        except OSError:
            return b""

    def _handle_frame(self, raw) -> bool:
        """False when the frame ends the session."""
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.debug("session %s ignored a frame: %s", self.sid, exc)
            return True
        if not isinstance(frame, dict):
            return True
        kind = frame.get("t")
        if kind == "in":
            data = _b64(frame.get("d"))
            if data is not None:
                self._write(data)
            return True
        if kind == "resize":
            self._resize(frame.get("cols"), frame.get("rows"))
            return True
        if kind == "close":
            self._end(str(frame.get("reason") or "closed"))
            return False
        return True

    def _write(self, data: bytes) -> None:
        if self._fd is not None:
            os.write(self._fd, data)
        elif self._sock is not None:
            self._sock.sendall(data)
        elif self._winpty is not None:
            self._winpty.write(data.decode("utf-8", "replace"))
        elif self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def _resize(self, cols, rows) -> None:
        if not isinstance(cols, int) or not isinstance(rows, int):
            return
        cols = min(max(cols, 1), 500)
        rows = min(max(rows, 1), 300)
        if self._fd is not None and self._child:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        elif self._winpty is not None:
            try:
                self._winpty.setwinsize(rows, cols)
            except OSError as exc:
                logger.debug("session %s resize failed: %s", self.sid, exc)

    # -- cleanup -------------------------------------------------------------

    def _cleanup(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._reader is not None:
            self._reader.stop()
        if self._child and self._pid is not None:
            _reap(self._pid)
            self._child = False
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as exc:
                logger.debug("session %s close pty: %s", self.sid, exc)
            self._fd = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError as exc:
                logger.debug("session %s close socket: %s", self.sid, exc)
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self._stopping.set()


def _login_shell_argv(config) -> list:
    """The configured account's own login shell. /bin/sh stands in when the
    account does not exist: an operator should get a session they can look at
    rather than silence."""
    try:
        pw = pwd.getpwnam(config.remote_shell_user)
    except KeyError:
        pw = None
    return [getattr(pw, "pw_shell", None) or _LOGIN_SHELL_FALLBACK, "-l"]


# ---------------------------------------------------------------------------
# Output readers (Windows only — POSIX pumps with select)
# ---------------------------------------------------------------------------

class _OutputReader(threading.Thread):
    """Copies local output to the WebSocket on its own thread.

    Needed wherever ``select`` cannot watch the local end: a Windows shell's
    pipes, and — on any host — RDP's socket, which is a peer the pump otherwise
    reads with ``os.read``."""

    @property
    def exited(self) -> bool:
        return self._stopping.is_set()

    def __init__(self, ws, poll_seconds: float,
                 peer_stopped: threading.Event | None = None):
        super().__init__(daemon=True)
        self.ws = ws
        self.peer_stopped = peer_stopped
        self.poll_seconds = poll_seconds
        self._winpty = None
        self._proc = None
        self._sock: socket.socket | None = None
        self._stopping = threading.Event()

    def bind(self, winpty, proc, sock) -> None:
        self._winpty = winpty
        self._proc = proc
        self._sock = sock

    def stop(self) -> None:
        self._stopping.set()
        # Waking the socket is the only way into a blocked recv(); closing it is
        # the session's business, so let the socket die on its own first.
        if self.peer_stopped is not None:
            self.peer_stopped.set()

    def run(self) -> None:
        try:
            while not self._stopping.is_set():
                data = self._read()
                if not data:
                    break
                self.ws.send(json.dumps({"t": "out",
                                         "d": base64.b64encode(data).decode()}))
        except OSError as exc:
            logger.debug("session output reader ended: %s", exc)
        finally:
            self._stopping.set()

    def _read(self) -> bytes:
        if self._sock is not None:
            return self._sock.recv(_PIPE_READ)
        if self._winpty is not None:
            return self._winpty.read(_PIPE_READ).encode("utf-8", "replace")
        if self._proc is not None and self._proc.stdout is not None:
            return self._proc.stdout.read(_PIPE_READ)
        self._stopping.wait(self.poll_seconds)
        return b""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _child_setup(argv: list, drop_user: bool, user: str) -> None:
    """Runs in the forked child, and never returns: it execs, or it exits 1.

    No logging here. The agent is multi-threaded, and a lock another thread
    held at fork time stays held forever in the child. The shell gets a login
    environment built here rather than inherited: the agent's environment is a
    service manager's, and a root service must not hand its surroundings to a
    session about to run as somebody else. Any failure to drop to the account,
    including an account that does not exist, ends the child: never a shell as
    the wrong user."""
    try:
        home = os.environ.get("HOME", "/")
        if drop_user:
            pw = pwd.getpwnam(user)
            # Already the account we are told to become (a root agent running a
            # root shell, or an unprivileged agent as that user): nothing to drop.
            if os.geteuid() != pw.pw_uid:
                os.initgroups(user, pw.pw_gid)
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)
            home = pw.pw_dir or "/"
        try:
            os.chdir(home)
        except OSError:
            os.chdir("/")
        env = {
            "TERM": "xterm-256color",
            "HOME": home,
            "USER": user if drop_user else os.environ.get("USER", ""),
            "LOGNAME": user if drop_user else os.environ.get("LOGNAME", ""),
            "SHELL": argv[0],
            "PATH": _TERM_PATH,
        }
        os.execvpe(argv[0], argv, env)
    except BaseException:  # noqa: BLE001 — a child must never return into the agent
        pass
    os._exit(1)


def _b64(value):
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value.encode(), validate=True)
    except (binascii.Error, ValueError):
        return None


def _reap(pid: int) -> None:
    """SIGHUP the child's group, then escalate. A shell that ignores SIGHUP
    must not outlive the browser that opened it."""
    try:
        os.killpg(pid, signal.SIGHUP)
    except OSError as exc:
        logger.debug("session child %s already gone: %s", pid, exc)
    for _ in range(20):
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError as exc:
        logger.debug("session child %s unkillable: %s", pid, exc)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
