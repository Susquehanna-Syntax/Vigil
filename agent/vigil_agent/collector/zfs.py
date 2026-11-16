"""ZFS pool health from the ``zpool`` command.

Runs wherever ``zpool`` exists: FreeBSD, OPNsense, Linux with OpenZFS. A host
without it, which is the ordinary case, returns an empty list silently. Two
commands run per scrape: ``zpool list`` for capacity and state, and
``zpool status`` for error counts and the last scrub. Neither is allowed to
raise; a failure is logged once and that part of the report is skipped.
"""

import re
from datetime import datetime

from ..procenv import clean_env
from ._common import _point, _warn_once, shutil, subprocess

_ZPOOL_TIMEOUT = 10

_LIST_FIELDS = "name,size,alloc,free,frag,cap,health"

#: Pool health word → the numeric code the dashboard charts.
_STATE_CODES = {
    "ONLINE": 0, "DEGRADED": 1, "FAULTED": 2, "OFFLINE": 3,
    "UNAVAIL": 4, "REMOVED": 5, "SUSPENDED": 6,
}
_UNKNOWN_STATE = 7

_POOL_LINE = re.compile(r"^\s*pool:\s*(\S+)\s*$")
_CONFIG_HEADER = re.compile(r"^\s*NAME\s+STATE\s+READ\s+WRITE\s+CKSUM\s*$")
_SCAN_LINE = re.compile(r"^\s*scan:\s*(.*?)\s*$")
_SCRUB_DONE = re.compile(r"^scrub repaired \S+ in \S+ with \d+ errors? on (.+)$")
_SCRUB_RUNNING = re.compile(r"^scrub in progress since (.+)$")
_SCRUB_NEVER = "none requested"
_DATE_FORMAT = "%a %b %d %H:%M:%S %Y"


def collect_zfs(now: datetime | None = None) -> list[dict]:
    """Pool capacity, state, error totals and scrub age, one set per pool.

    ``now`` is the reference time for the scrub age; it defaults to the local
    clock and exists so tests can freeze it.
    """
    if not shutil.which("zpool"):
        return []
    try:
        listing = _run_zpool(["list", "-Hp", "-o", _LIST_FIELDS])
        if listing is None:
            return []
        points = _list_points(listing)
        status = _run_zpool(["status", "-p"])
        if status is not None:
            points.extend(_status_points(status, now or datetime.now()))
        return points
    except Exception as exc:
        _warn_once("zfs collector", exc)
        return []


def _run_zpool(args: list[str]) -> str | None:
    """Run one zpool subcommand, or return None if it timed out or failed."""
    what = f"zpool {args[0]}"
    try:
        proc = subprocess.run(
            ["zpool", *args], capture_output=True, text=True,
            timeout=_ZPOOL_TIMEOUT, env=clean_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _warn_once(what, exc)
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        _warn_once(what, RuntimeError(f"exited {proc.returncode}: {detail}"))
        return None
    return proc.stdout


def _list_points(out: str) -> list[dict]:
    """One set of capacity and state points per line of ``zpool list -Hp``."""
    points = []
    for line in out.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        try:
            if len(cells) != 7:
                raise ValueError(f"expected 7 fields, got {len(cells)}: {line!r}")
            name, size, alloc, free, frag, cap, health = cells
            size_v, alloc_v, free_v, cap_v = int(size), int(alloc), int(free), int(cap)
            # "-" is zpool's blank for a fragmentation figure it cannot give.
            frag_v = None if frag == "-" else int(frag)
        except ValueError as exc:
            _warn_once("zpool list", exc)
            continue
        labels = {"pool": name}
        points.append(_point("zfs", "pool_size_bytes", size_v, labels))
        points.append(_point("zfs", "pool_used_bytes", alloc_v, labels))
        points.append(_point("zfs", "pool_free_bytes", free_v, labels))
        points.append(_point("zfs", "pool_capacity_percent", cap_v, labels))
        if frag_v is not None:
            points.append(_point("zfs", "pool_fragmentation_percent", frag_v, labels))
        points.append(_point("zfs", "pool_healthy", 1 if health == "ONLINE" else 0, labels))
        state_labels = {"pool": name, "state": health}
        code = _STATE_CODES.get(health, _UNKNOWN_STATE)
        points.append(_point("zfs", "pool_state", code, state_labels))
    return points


def _status_points(out: str, now: datetime) -> list[dict]:
    """Error totals and scrub age per pool, from ``zpool status -p``."""
    points = []
    for name, lines in _split_pools(out).items():
        labels = {"pool": name}
        errors = _config_errors(lines)
        if errors is not None:
            points.append(_point("zfs", "pool_errors", errors, labels))
        points.extend(_scan_points(lines, labels, now))
    return points


def _split_pools(out: str) -> dict[str, list[str]]:
    """Group the status output into one list of lines per ``pool:`` block."""
    pools: dict[str, list[str]] = {}
    current = None
    for line in out.splitlines():
        match = _POOL_LINE.match(line)
        if match:
            current = match.group(1)
            pools[current] = []
        elif current is not None:
            pools[current].append(line)
    return pools


def _config_errors(lines: list[str]) -> int | None:
    """READ + WRITE + CKSUM summed over the leaf devices of the config table.

    The pool and vdev rows repeat their children's counts, so summing every
    row would count each error two or three times. Only leaves are summed.
    None when the block has no config table or a count is not an integer.
    """
    try:
        start = next(i + 1 for i, line in enumerate(lines) if _CONFIG_HEADER.match(line))
    except StopIteration:
        return None
    rows = []
    for line in lines[start:]:
        if not line.strip():
            break
        rows.append(line.expandtabs(8))
    depths = [len(row) - len(row.lstrip()) for row in rows]
    total = 0
    try:
        for i, row in enumerate(rows):
            if i + 1 < len(rows) and depths[i + 1] > depths[i]:
                continue  # a pool or vdev row: its counts are its children's
            cells = row.split()
            if len(cells) < 5:
                continue  # a section heading such as "logs" or "cache"
            total += sum(int(c) for c in cells[2:5])
    except ValueError as exc:
        _warn_once("zpool status config", exc)
        return None
    return total


def _scan_points(lines: list[str], labels: dict, now: datetime) -> list[dict]:
    """Scrub age from the ``scan:`` line. A resilver is not a scrub and gives none."""
    scan = None
    for line in lines:
        match = _SCAN_LINE.match(line)
        if match:
            scan = match.group(1)
            break
    if scan is None:
        return []
    if scan == _SCRUB_NEVER:
        return [_point("zfs", "scrub_age_days", -1, labels)]
    if scan.startswith("resilver"):
        return []
    running = _SCRUB_RUNNING.match(scan)
    if running:
        return [_point("zfs", "scrub_in_progress", 1, labels)]
    done = _SCRUB_DONE.match(scan)
    if done:
        try:
            finished = datetime.strptime(done.group(1), _DATE_FORMAT)
        except ValueError as exc:
            _warn_once("zpool status scan date", exc)
            return []
        days = round((now - finished).total_seconds() / 86400, 1)
        return [_point("zfs", "scrub_age_days", days, labels)]
    _warn_once("zpool status scan line", ValueError(f"unrecognised scan line: {scan!r}"))
    return []
