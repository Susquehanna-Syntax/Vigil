"""Sweep one private subnet and report what answered.

scan() takes a CIDR plus the ports to try and an optional SNMP community, and
returns one dict: the addresses probed and the hosts that answered, each with
its latency, open ports, reverse DNS name and (when a community is given) its
sysName and sysDescr.

Two limits keep an agent from becoming an internet scanner. The network must
be a *private* IPv4 network, and it must be at most ``MAX_ADDRESSES`` (1024, a
/22) addresses; a larger network is refused before anything is sent, never cut
down to fit. A scan also stops at a deadline: each worker checks it before it
probes an address, so addresses past it are not probed and the result says
``truncated: True``.

Nothing here sends anything but: an ICMP echo (through netcheck's own ping), a
TCP connect to each listed port, one SNMP GET per answering host when a
community is given, and a reverse DNS lookup. Every rejection is a
DiscoveryError raised before the first probe; nothing that happens afterwards
can fail the scan.
"""

from __future__ import annotations

import base64
import concurrent.futures
import ipaddress
import logging
import math
import socket
import threading
import time

from nacl.exceptions import BadSignatureError

from vigil_agent import netcheck
from vigil_agent.failover import _canonical
from vigil_agent.snmp import SnmpError, V2cClient
from vigil_agent.verify import agent_fingerprint

logger = logging.getLogger("vigil.discovery")

MAX_ADDRESSES = 1024  # a /22; a larger network is refused, not cut
DEFAULT_PORTS = (22, 23, 80, 443, 445, 3389, 8080, 8443)
MAX_PORTS = 16
DEFAULT_TIMEOUT = 1.0  # per probe, seconds
DEFAULT_WORKERS = 32
DEFAULT_DEADLINE = 300.0  # the whole scan, seconds
SYSDESCR_OID = "1.3.6.1.2.1.1.1.0"
MAX_TEXT = 100  # sys_name, sys_descr and name are cut to this

MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 5.0
MIN_WORKERS = 1
MAX_WORKERS = 64
MIN_DEADLINE = 1.0
MAX_DEADLINE = 900.0


class DiscoveryError(ValueError):
    """A scan request that is refused before anything is sent."""


def scan(
    cidr: str,
    *,
    ports=DEFAULT_PORTS,
    community: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    deadline: float = DEFAULT_DEADLINE,
) -> dict:
    """Probe every address in a private network and describe the hosts that answered."""
    net = _valid_network(cidr)
    probe_ports = _valid_ports(ports)
    timeout = _clamp(timeout, MIN_TIMEOUT, MAX_TIMEOUT, DEFAULT_TIMEOUT)
    workers = int(_clamp(workers, MIN_WORKERS, MAX_WORKERS, DEFAULT_WORKERS))
    deadline = _clamp(deadline, MIN_DEADLINE, MAX_DEADLINE, DEFAULT_DEADLINE)

    hosts = [str(ip) for ip in net.hosts()]
    start = time.monotonic()
    limit = start + deadline
    found: list[dict] = []
    scanned = 0
    truncated = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_probe, address, probe_ports, timeout, community, limit)
                   for address in hosts]
        for future in concurrent.futures.as_completed(futures):
            probed, host = future.result()
            if not probed:
                truncated = True
                continue
            scanned += 1
            if host is not None:
                found.append(host)

    found.sort(key=lambda host: ipaddress.ip_address(host["ip"]))
    return {
        "cidr": str(net),
        "scanned": scanned,
        "found": found,
        "truncated": truncated,
        "seconds": round(time.monotonic() - start, 1),
    }


# ---------------------------------------------------------------- validation


def _valid_network(cidr: object) -> ipaddress.IPv4Network:
    if not isinstance(cidr, str):
        raise DiscoveryError("not a network: a CIDR string is required")
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise DiscoveryError(f"not a network: {exc}") from None
    if net.version != 4:
        raise DiscoveryError("only IPv4 networks can be scanned")
    if not net.is_private or net.is_loopback or net.is_multicast or net.is_unspecified:
        raise DiscoveryError("only private networks can be scanned")
    if net.num_addresses > MAX_ADDRESSES:
        raise DiscoveryError(f"at most {MAX_ADDRESSES} addresses (a /22) per scan")
    return net


def _valid_ports(ports: object) -> tuple[int, ...]:
    if not isinstance(ports, (list, tuple)) or not 1 <= len(ports) <= MAX_PORTS:
        raise DiscoveryError("invalid ports")
    chosen: list[int] = []
    for port in ports:
        if not isinstance(port, int) or isinstance(port, bool):
            raise DiscoveryError("invalid ports")
        if not 1 <= port <= 65535:
            raise DiscoveryError("invalid ports")
        if port not in chosen:
            chosen.append(port)
    return tuple(chosen)


def _clamp(value: object, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return min(max(number, low), high)


# ---------------------------------------------------------------- the probes


def _probe(address: str, ports: tuple[int, ...], timeout: float,
           community: str | None, limit: float) -> tuple[bool, dict | None]:
    """One address: (probed, host). Not probed once the deadline has passed;
    host is None when nothing answered. Never raises."""
    if time.monotonic() >= limit:
        return False, None
    try:
        host = _probe_host(address, ports, timeout)
        if host is not None and community:
            _fill_snmp(host, community, timeout)
        return True, host
    except Exception:  # noqa: BLE001 - one odd host must never fail the whole scan
        return True, None


def _probe_host(address: str, ports: tuple[int, ...], timeout: float) -> dict | None:
    latency_ms, by_ping = _ping(address, timeout)

    open_ports: list[int] = []
    alive = by_ping
    for port in ports:
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
        except ConnectionRefusedError:
            alive = True
            continue
        except OSError:
            continue
        sock.close()
        open_ports.append(port)
        alive = True

    if not alive:
        return None
    return {
        "ip": address,
        "alive_by": "ping" if by_ping else "tcp",
        "latency_ms": latency_ms,
        "open_ports": sorted(open_ports),
        "name": _reverse_dns(address),
        "sys_name": "",
        "sys_descr": "",
    }


def _ping(address: str, timeout: float) -> tuple[float | None, bool]:
    """(latency_ms, answered) from a single echo."""
    result = netcheck.run_check(
        {"kind": "icmp", "host": address, "count": 1}, timeout=timeout
    )
    if not result.get("ok"):
        return None, False
    latency = result.get("latency_ms")
    return (float(latency) if latency is not None else None), True


def _reverse_dns(address: str) -> str:
    try:
        name = socket.gethostbyaddr(address)[0]
    except OSError:
        return ""
    return _clean(name if isinstance(name, str) else "")


def _fill_snmp(host: dict, community: str, timeout: float) -> None:
    """sysName and sysDescr for one answering host. Silence leaves both empty."""
    try:
        values = V2cClient(
            host["ip"], community=community, timeout=timeout, retries=0
        ).get([netcheck.SYSNAME_OID, SYSDESCR_OID])
    except (SnmpError, OSError):
        return
    host["sys_name"] = _clean(_text(values.get(netcheck.SYSNAME_OID)))
    host["sys_descr"] = _clean(_text(values.get(SYSDESCR_OID)))


def _text(varbind: tuple[int, object] | None) -> str:
    if varbind is None:
        return ""
    value = varbind[1]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _clean(text: str) -> str:
    return "".join(c for c in text if c.isprintable())[:MAX_TEXT]


# ---------------------------------------------------------------- the scanner

MAX_DONE = 32  # answered ids remembered against a resend
_MAX_LOGGED = 1000  # the once-only log keys are forgotten past this many


def _start_thread(fn) -> None:
    threading.Thread(target=fn, name="vigil-discovery", daemon=True).start()


def _default_scan(cidr, ports=None, community=None):
    """``scan`` looked up per call, so patching the module reaches a Scanner."""
    return scan(cidr, ports=ports, community=community)


class Scanner:
    """Runs the discovery scan the dashboard signed for this agent.

    ``learn`` adopts a scan exactly as :class:`vigil_agent.poller.Poller` adopts
    a check list — only when its signature verifies against this agent's own
    fingerprint, and never an issue older than the one already taken. The scan
    runs on a thread of its own so the check-in loop is never blocked, one at a
    time; its answer waits in :meth:`take_result` for the next check-in and goes
    back to :meth:`restore_result` when that check-in fails. The server keeps
    handing out the same scan until its answer arrives, so an id already running,
    waiting to be sent or already sent is never run twice.
    """

    def __init__(self, config, *, run_scan=None, start_thread=None):
        self._config = config
        # Late module lookup so a Scanner built at startup still honours a
        # scan() patched later, as the rest of the agent's fakes do.
        self._run_scan = _default_scan if run_scan is None else run_scan
        self._start_thread = _start_thread if start_thread is None else start_thread
        self._lock = threading.Lock()
        self._running: str | None = None    # id of the scan being run
        self._result: dict | None = None    # the answer waiting to be sent
        self._done: list[str] = []          # ids already answered, newest last
        self._issued: int | None = None     # newest accepted issue time
        self._logged: set = set()           # log keys already written once

    # ── the scan ─────────────────────────────────────────────────────────

    def learn(self, response: dict, verify_key) -> None:
        """Start the scan the dashboard signed for this agent, if it verifies."""
        if not self._config.poller or verify_key is None or not isinstance(response, dict):
            return
        if not {"scan", "scan_issued", "scan_sig"} <= set(response):
            return
        entry, issued, signature = (response["scan"], response["scan_issued"],
                                    response["scan_sig"])
        if not isinstance(entry, dict) or not isinstance(issued, int) \
                or isinstance(issued, bool) or not isinstance(signature, str):
            self._warn_once(("malformed", None), "ignored a malformed scan")
            return
        payload = {
            "v": 1,
            "type": "discovery-scan",
            "agent": agent_fingerprint(self._config.agent_token),
            "issued": issued,
            "scan": entry,
        }
        try:
            verify_key.verify(_canonical(payload), base64.b64decode(signature))
        except (BadSignatureError, ValueError, TypeError):
            self._warn_once(("unsigned", issued), "ignored a scan that does not verify")
            return
        # A genuine issue older than the one in use is a replay: a scan that was
        # cancelled could otherwise start again.
        with self._lock:
            if self._issued is not None and issued < self._issued:
                logger.warning("ignored a scan older than the one in use")
                return
            self._issued = issued

        entry = _clean_entry(entry)
        if entry is None:
            self._warn_once(("malformed", issued), "ignored a malformed scan")
            return
        scan_id = entry["id"]
        with self._lock:
            # Re-check under the lock: a newer issue may have landed meanwhile.
            if self._issued is not None and issued < self._issued:
                return
            if scan_id == self._running or scan_id == self._answer_id() \
                    or scan_id in self._done:
                return
            if self._running is not None:
                return
            self._running = scan_id
        self._start_thread(lambda: self._run(entry))

    def _answer_id(self) -> str | None:
        """The id of the answer waiting to be sent, callers holding the lock."""
        answer = self._result
        return answer.get("id") if isinstance(answer, dict) else None

    # ── the thread ───────────────────────────────────────────────────────

    def _run(self, entry: dict) -> None:
        cidr = entry["cidr"]
        try:
            result = self._run_scan(cidr, ports=entry["ports"],
                                    community=entry.get("community"))
            answer = {"id": entry["id"], **result}
        except DiscoveryError as exc:
            answer = {"id": entry["id"], "error": str(exc)}
        except Exception:  # the answer must never carry a traceback
            logger.exception("discovery scan of %s failed", cidr)
            answer = {"id": entry["id"], "error": "the scan failed on the agent"}
        if "error" in answer:
            logger.info("discovery scan of %s failed: %s", cidr, answer["error"])
        else:
            logger.info("discovery scan of %s finished: %s found of %s",
                        cidr, _count(answer.get("found")), _count(answer.get("scanned")))
        with self._lock:
            # Published only while this is still the running scan: an answer
            # that arrived after its check-in had already given up on it would
            # otherwise be sent twice, and the next scan could never start.
            if self._running == entry["id"]:
                self._result = answer
                self._running = None

    # ── the answer ───────────────────────────────────────────────────────

    def take_result(self) -> dict | None:
        """The scan result waiting for the next check-in, once."""
        with self._lock:
            answer = self._result
            if answer is None:
                return None
            self._result = None
            self._remember(answer)
            return answer

    def restore_result(self, answer: dict) -> None:
        """A check-in failed: put this answer back so the next one retries it."""
        if not isinstance(answer, dict):
            return
        with self._lock:
            if self._result is not None:
                return
            self._result = answer
            scan_id = answer.get("id")
            if isinstance(scan_id, str) and scan_id in self._done:
                self._done.remove(scan_id)

    # ── logging ──────────────────────────────────────────────────────────

    def _remember(self, answer: dict) -> None:
        scan_id = answer.get("id")
        if not isinstance(scan_id, str):
            return
        if scan_id in self._done:
            self._done.remove(scan_id)
        self._done.append(scan_id)
        del self._done[:-MAX_DONE]

    def _warn_once(self, key, message: str) -> None:
        with self._lock:
            if key in self._logged:
                return
            if len(self._logged) >= _MAX_LOGGED:
                self._logged.clear()
            self._logged.add(key)
        logger.warning(message)


def _count(value: object) -> object:
    return len(value) if isinstance(value, list) else value


def _clean_entry(entry: dict) -> dict | None:
    """The scan entry to run, None for anything malformed."""
    scan_id, cidr = entry.get("id"), entry.get("cidr")
    ports, community = entry.get("ports"), entry.get("community")
    if not isinstance(scan_id, str) or not scan_id:
        return None
    if not isinstance(cidr, str) or not isinstance(ports, list):
        return None
    if community is not None and not isinstance(community, str):
        return None
    clean = {"id": scan_id, "cidr": cidr, "ports": ports}
    if community is not None:
        clean["community"] = community
    return clean
