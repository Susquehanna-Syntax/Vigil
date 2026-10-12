"""What this machine looks like on the network: addresses, sockets, peers (14.4).

The topology view needs, per host, every interface address, what the host
listens on, and who it talks to. The check-in carries it every 5 minutes and
the server keeps one snapshot per host (``HostNetwork``).

Never raises. Interfaces come from ``net_if_addrs``/``net_if_stats``, sockets
from ``net_connections``; the latter needs root on macOS, and its
``AccessDenied`` is reported as ``partial: true`` rather than dropping the
addresses the unprivileged call already returned.
"""

import ipaddress
import logging
import socket
from collections import Counter

import psutil

logger = logging.getLogger("vigil.netmap")

# The hardware-address family is 17 everywhere it exists, but not in socket's
# namespace on Linux — psutil's own constant is the portable one.
_AF_LINK = psutil.AF_LINK
# The caps the server's ingest keeps (apps/hosts/views.py); a longer list from
# a strange machine would only be truncated there anyway.
MAX_INTERFACES = 32
MAX_ADDR_PER_INTERFACE = 16
MAX_LISTENING = 256
MAX_CONNECTIONS = 300
MAX_NAME = 64


def collect_network() -> dict | None:
    """This host's network snapshot, or None when not even interfaces read."""
    try:
        interfaces = _interfaces()
    except Exception:
        logger.exception("network collection failed")
        return None
    partial = False
    conns: list = []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        # macOS without root: the addresses above are still worth sending.
        return {"interfaces": interfaces, "listening": [], "connections": [],
                "partial": True}
    except Exception:
        logger.exception("socket enumeration failed")
        partial = True
    processes = _process_names(conns)
    listening = _listening(conns, processes)
    connections = _connections(conns, listening, processes)
    return {"interfaces": interfaces, "listening": listening,
            "connections": connections, "partial": partial}


def _interfaces() -> list:
    """Per interface: whether it's up, its hardware address, its unicast addrs."""
    addrs = psutil.net_if_addrs()
    try:
        stats = psutil.net_if_stats()
    except Exception:
        logger.exception("interface state enumeration failed")
        stats = {}

    out: list = []
    for name, entries in list(addrs.items())[:MAX_INTERFACES]:
        mac = ""
        addresses: list = []
        for entry in entries or []:
            family = getattr(entry, "family", None)
            if family == _AF_LINK:
                if not mac:
                    mac = _text(getattr(entry, "address", ""), 32)
                continue
            addr = _address(entry, family)
            if addr is not None and len(addresses) < MAX_ADDR_PER_INTERFACE:
                addresses.append(addr)
        out.append({"name": _text(name, MAX_NAME),
                    "up": bool(getattr(stats.get(name), "isup", False)),
                    "mac": mac,
                    "addrs": addresses})
    return out


def _address(entry, family) -> dict | None:
    """One unicast address with its prefix length, or None to skip it."""
    if family == socket.AF_INET:
        family_name, prefix = "ipv4", _prefix_v4(getattr(entry, "netmask", None))
    elif family == socket.AF_INET6:
        family_name, prefix = "ipv6", _prefix_v6(getattr(entry, "netmask", None))
    else:
        return None
    raw = _text(getattr(entry, "address", ""), 64)
    if "%" in raw:                       # link-local scope id: fe80::1%eth0
        raw = raw.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if ip.is_loopback:
        return None
    return {"family": family_name, "address": str(ip), "prefix": prefix}


def _prefix_v4(netmask) -> int | None:
    if not netmask:
        return None
    try:
        return ipaddress.ip_network(f"0.0.0.0/{netmask}", strict=False).prefixlen
    except ValueError:
        return None


def _prefix_v6(netmask) -> int | None:
    if not netmask:
        return None
    # The same arithmetic ip_network does for an IPv4 netmask, on a host of
    # 128 bits: ipaddress refuses a v6 mask written as a v6 network.
    try:
        bits = int(str(netmask).replace(":", ""), 16)
    except ValueError:
        return None
    return bits.bit_count()


def _process_names(conns) -> dict:
    """pid → process name. One lookup per pid: ``net_connections`` repeats a
    pid across its sockets and ``Process()`` is a syscall."""
    names: dict = {}
    for conn in conns:
        pid = getattr(conn, "pid", None)
        if pid is None or pid in names:
            continue
        try:
            names[pid] = _text(psutil.Process(pid).name(), MAX_NAME)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            names[pid] = ""
    return names


def _proto(conn) -> str:
    return "udp" if conn.type == socket.SOCK_DGRAM else "tcp"


def _listening(conns, processes: dict) -> list:
    """Sockets accepting traffic: TCP in LISTEN, UDP bound to a port."""
    seen: set = set()
    for conn in conns:
        proto = _proto(conn)
        laddr = conn.laddr
        port = getattr(laddr, "port", 0) or 0
        if proto == "tcp":
            if conn.status != psutil.CONN_LISTEN:
                continue
        elif not port:
            continue
        seen.add((proto, _text(getattr(laddr, "ip", ""), 64), port,
                  processes.get(conn.pid, "")))
    out = [{"proto": proto, "address": address, "port": port, "process": process}
           for proto, address, port, process in seen]
    out.sort(key=lambda r: (r["port"], r["proto"], r["address"]))
    return out[:MAX_LISTENING]


def _connections(conns, listening: list, processes: dict) -> list:
    """Live TCP sessions to somewhere that isn't this machine, aggregated: 200
    connections to one database are one row with count 200."""
    listen_ports = {r["port"] for r in listening if r["proto"] == "tcp"}
    counts: Counter = Counter()
    for conn in conns:
        if conn.status != psutil.CONN_ESTABLISHED:
            continue
        raddr = conn.raddr
        remote = _text(getattr(raddr, "ip", ""), 64)
        if not remote:
            continue
        try:
            if ipaddress.ip_address(remote).is_loopback:
                continue
        except ValueError:
            continue
        local_port = getattr(conn.laddr, "port", 0) or 0
        # An inbound session is one that landed on a port we listen on. Its
        # ephemeral remote port differs per session and would defeat the
        # count, so only the local port identifies the aggregate.
        direction = "in" if local_port in listen_ports else "out"
        remote_port = getattr(raddr, "port", 0) or 0
        counts[(_proto(conn), direction, remote,
                0 if direction == "in" else remote_port,
                local_port if direction == "in" else 0,
                processes.get(conn.pid, ""))] += 1

    out = [{"proto": proto, "direction": direction, "remote_ip": remote,
            "remote_port": remote_port, "local_port": local_port,
            "process": process, "count": count}
           for (proto, direction, remote, remote_port, local_port, process),
               count in counts.items()]
    out.sort(key=lambda r: (-r["count"], r["direction"], r["remote_ip"],
                            r["remote_port"], r["local_port"], r["proto"]))
    return out[:MAX_CONNECTIONS]


def _text(value, limit: int) -> str:
    return str(value if value is not None else "")[:limit]
