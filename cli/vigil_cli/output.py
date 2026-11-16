"""Plain-text tables and relative times. No colour, no box drawing: the output
is as readable piped into a file as it is on a terminal."""
from __future__ import annotations

import re
from datetime import datetime, timezone

#: Everything a terminal acts on rather than shows: the C0 and C1 control
#: characters (escape among them) other than newline and tab, DEL, and the
#: bidirectional overrides that reorder what is displayed.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def safe(text: str) -> str:
    """*text* with every control character made visible. Host names, alert
    messages and task output come from agents: a compromised host must not be
    able to move the cursor, recolour or rewrite an operator's terminal."""
    return _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()) if ord(m.group()) < 0x100
                        else "\\u%04x" % ord(m.group()), text)


def table(headers: list[str], rows: list[list], *, empty: str = "Nothing to show.") -> str:
    if not rows:
        return empty
    cells = [[_cell(c) for c in row] for row in rows]
    widths = [max(len(headers[i]), *(len(r[i]) for r in cells)) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(lines)


def _cell(value) -> str:
    # One row is one line, whatever a message or an output holds.
    return " ".join(str("-" if value is None or value == "" else value).split())


def ago(iso, *, now: datetime | None = None) -> str:
    """"3m ago" for an ISO timestamp, "never" for none."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    if seconds < 0:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"
