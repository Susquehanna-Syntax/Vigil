"""Sweep one private subnet and report what answered.

scan() takes a CIDR plus the ports to try and an optional SNMP community, and
returns one dict: the addresses probed and the hosts that answered, each with
its latency, open ports, reverse DNS name and (when a community is given) its
sysName and sysDescr.

Two limits keep an agent from becoming an internet scanner. The network must
be a *private* IPv4 network, and it must be at most ``MAX_ADDRESSES`` (1024, a
/22) addresses; a larger network is refused before anything is sent, never cut
down to fit. A scan also stops at a deadline: addresses past it are simply not
probed and the result says ``truncated: True``.

Nothing here sends anything but: an ICMP echo (through netcheck's own ping), a
TCP connect to each listed port, one SNMP GET per answering host when a
community is given, and a reverse DNS lookup. Every rejection is a
DiscoveryError raised before the first probe; nothing that happens afterwards
can fail the scan.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import math
import socket
import time

from vigil_agent import netcheck
from vigil_agent.snmp import SnmpError, V2cClient

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
        futures: list[concurrent.futures.Future] = []
        for address in hosts:
            if time.monotonic() >= limit:
                truncated = True
                break
            futures.append(pool.submit(_probe, address, probe_ports, timeout))
        scanned = len(futures)
        for future in concurrent.futures.as_completed(futures):
            host = future.result()
            if host is not None:
                found.append(host)

    if community:
        for host in found:
            _fill_snmp(host, community, timeout)

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


def _probe(address: str, ports: tuple[int, ...], timeout: float) -> dict | None:
    """One address. None when nothing answered; never raises."""
    try:
        return _probe_host(address, ports, timeout)
    except Exception:  # noqa: BLE001 - one odd host must never fail the whole scan
        return None


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
