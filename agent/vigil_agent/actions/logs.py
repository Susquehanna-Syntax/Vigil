"""container_logs, and the live tail behind the log view (M11).

The action returns the container's last lines. When it carries a ``session``
the agent then keeps polling that container every ``POLL_SECONDS`` and posts
whatever is new, until the server says the viewer has gone, the session
reaches ``MAX_SECONDS``, or the server stops answering. Outbound only: the
server never connects to the agent.
"""

from __future__ import annotations

import re
import struct
import threading
import time

from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _validate_name

POLL_SECONDS = 1.5
MAX_SECONDS = 600
MAX_TAIL = 2000
_SESSION = re.compile(r"^[0-9a-f-]{36}$")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s?")


def demux(raw: bytes) -> list[str]:
    """Lines from an engine log body — multiplexed (8-byte frame headers,
    no TTY) or raw (a TTY container)."""
    out = b""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0":
        pos = 0
        while pos + 8 <= len(raw):
            size = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
            out += raw[pos + 8:pos + 8 + size]
            pos += 8 + size
    else:
        out = raw
    return [line for line in out.decode("utf-8", "replace").splitlines() if line]


def _since(ts_line: str) -> str | None:
    """The engine's ``since`` (unix seconds, nanosecond fraction) of a line's
    timestamp — the boundary line comes back again and is dropped by the
    caller."""
    from datetime import datetime, timezone

    match = _TS.match(ts_line)
    if not match:
        return None
    whole, _, frac = match.group(1).rstrip("Z").partition(".")
    base = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return f"{int(base.timestamp())}.{(frac + '000000000')[:9]}"


def fetch(name: str, *, tail: int | None = None, since: str | None = None) -> list[str]:
    status, body, _ = ex._engine().raw(
        "GET", f"/containers/{name}/logs",
        query={"stdout": 1, "stderr": 1, "timestamps": 1,
               "tail": tail if tail is not None else None, "since": since})
    if status != 200:
        raise RuntimeError(f"logs for {name}: engine answered {status}")
    return demux(body)


def _tail_loop(config: AgentConfig, name: str, session: str, last: str | None,
               sleep=time.sleep, clock=time.monotonic) -> int:
    """Poll and ship new lines until told to stop; returns polls made."""
    from .. import client

    started = clock()
    polls = 0
    while clock() - started < MAX_SECONDS:
        sleep(POLL_SECONDS)
        polls += 1
        try:
            lines = fetch(name, since=_since(last) if last else None)
        except Exception:  # noqa: BLE001 — the container went away: stop
            return polls
        if last and lines and lines[0] == last:
            lines = lines[1:]   # `since` is inclusive of the boundary line
        if lines:
            last = lines[-1]
        if not client.post_log_lines(config, session, lines[-500:]):
            return polls
    return polls


def _start_tail(config: AgentConfig, name: str, session: str, last: str | None) -> None:
    threading.Thread(target=_tail_loop, args=(config, name, session, last),
                     daemon=True, name=f"log-tail-{name}").start()


def _container_logs(params: dict, config: AgentConfig) -> str:
    name = _validate_name(params.get("container_name", ""), "container name")
    try:
        tail = int(params.get("tail", 200))
    except (TypeError, ValueError):
        raise ValueError("tail must be a number") from None
    tail = max(1, min(tail, MAX_TAIL))
    session = str(params.get("session") or "")
    if session and not _SESSION.match(session):
        raise ValueError("session must be a live-tail session id")
    lines = fetch(name, tail=tail)
    if session:
        _start_tail(config, name, session, lines[-1] if lines else None)
    return ActionOutput("\n".join(lines), {"lines": len(lines)})
