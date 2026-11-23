"""Basic checks for devices that cannot run an agent.

run_check() takes one check description (a dict with a "kind") and returns a
plain result dict. It never raises: every failure, including a malformed
check, becomes a result with ok False and a short detail sentence.

Kinds: "icmp" (system ping), "tcp" (connect), "http" (status, plus TLS
certificate expiry for https), "dns" (name resolution), "snmp" (SNMP v2c GET),
"ssh" (one read-only command on a device with a private key).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from requests.certs import where as _default_ca_bundle

from vigil_agent import snmp
from vigil_agent.procenv import clean_env

# Every limit lives here. Values outside a limit are clamped, not rejected.
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 30.0  # one check never holds a worker longer than this
ICMP_DEFAULT_COUNT = 3
ICMP_MAX_COUNT = 10  # echo requests per ping run
MAX_HOST_LENGTH = 253  # DNS name length limit
DEFAULT_CERT_DAYS = 14  # warn when a verified certificate has fewer days left
SYSNAME_OID = "1.3.6.1.2.1.1.5.0"  # the agent's own name, always fetched
SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"  # hundredths of a second since it booted
MAX_SNMP_VALUE_CHARS = 100  # a string result value is cut to this
MAX_SNMP_OIDS = 8  # labelled OIDs per SNMP check, the server's own limit
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"       # ifName
IF_HC_IN_OID = "1.3.6.1.2.1.31.1.1.1.6"      # ifHCInOctets (Counter64)
IF_HC_OUT_OID = "1.3.6.1.2.1.31.1.1.1.10"    # ifHCOutOctets (Counter64)
IF_HIGH_SPEED_OID = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed, Mbit/s
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"          # ifOperStatus: 1 up, 2 down, others unknown
MAX_INTERFACES = 64
SSH_TIMEOUT = 90.0  # an ssh check's whole run: key auth plus one command on a slow device
# the output of `show running-config` on the largest firewalls, in bytes; over it, the result is a failure
SSH_MAX_OUTPUT = 1_000_000
MAX_SSH_COMMAND_CHARS = 500  # one read-only command; more than this is not a command

_CERT_TIME_FORMAT = "%b %d %H:%M:%S %Y %Z"  # getpeercert() notAfter, e.g. "Jun  1 12:00:00 2027 GMT"
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")
_SSH_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# the base64 of a wire-format host key: standard alphabet, optional padding
_SSH_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_SSH_KEY_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"
_SSH_MAX_KEY_BYTES = 16_384
_SSH_HOST_KEY_TYPES = frozenset({
    "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "rsa-sha2-256", "rsa-sha2-512", "ssh-rsa",
})
# ssh's own wording, upper-cased so the match does not depend on its locale
_SSH_KEY_CHANGED_MARKERS = (
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
    "HOST KEY VERIFICATION FAILED",
)
# a command is one argv token for one CLI session; nothing that could end it.
# \x00 cannot be passed in an argv element at all, so the check catches a client
# or a device object that slipped something in before ssh ever sees E2BIG.
_SSH_COMMAND_FORBIDDEN = ("\n", "\r", "\x00")

_PING_SUMMARY_RE = re.compile(
    r"(\d+) packets? transmitted, (\d+) (?:packets? )?received.*?([\d.]+)% packet loss"
)
_PING_WINDOWS_RE = re.compile(r"Sent = (\d+), Received = (\d+), Lost = \d+ \(([\d.]+)% loss\)")
_PING_UNIX_AVG_RE = re.compile(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = [\d.]+/([\d.]+)/")
_PING_WINDOWS_AVG_RE = re.compile(r"Average = (\d+)ms")


class _CheckError(Exception):
    """A check that is invalid as given. The message is the result detail."""


def _new_extras(kind: str) -> dict:
    """Kind-specific keys, present on every result (success or failure)."""
    if kind == "icmp":
        return {"loss_percent": None, "sent": 0, "received": 0}
    if kind == "tcp":
        return {"port": None}
    if kind == "http":
        return {"status": None, "cert_days_left": None, "cert_expires": None}
    if kind == "dns":
        return {"addresses": []}
    if kind == "snmp":
        return {"sys_name": None, "uptime_s": None}
    if kind == "ssh":
        return {"host_key": None, "sha256": None, "size": None, "output": None}
    return {}


def _result(kind, target, ok, detail, latency_s=None, **extra) -> dict:
    out = {
        "kind": kind,
        "target": target,
        "ok": bool(ok),
        "latency_ms": None if latency_s is None else round(latency_s * 1000, 1),
        "detail": detail,
    }
    out.update(_new_extras(kind))
    out.update(extra)
    return out


def _clamp_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT
    return min(value, MAX_TIMEOUT)


def _fmt_seconds(timeout: float) -> str:
    return f"{timeout:g}"


# ---------------------------------------------------------------- field checks


def _str_field(check: dict, key: str) -> str:
    value = check.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _CheckError(f"missing {key}")
    return value


def _bool_field(check: dict, key: str, default: bool) -> bool:
    value = check.get(key, default)
    if not isinstance(value, bool):
        raise _CheckError(f"invalid {key}")
    return value


def _port_field(check: dict) -> int:
    value = check.get("port")
    if value is None:
        raise _CheckError("missing port")
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise _CheckError("invalid port")
    return value


def _status_field(check: dict) -> list[int] | None:
    value = check.get("expect_status")
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, list) and all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        return list(value)
    raise _CheckError("invalid expect_status")


def _expect_field(check: dict) -> list[str] | None:
    value = check.get("expect")
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise _CheckError("invalid expect")


def _days_field(check: dict) -> int:
    value = check.get("expect_cert_days", DEFAULT_CERT_DAYS)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _CheckError("invalid expect_cert_days")
    return value


# ---------------------------------------------------------------- icmp


def _valid_host(host: str) -> bool:
    return (
        0 < len(host) <= MAX_HOST_LENGTH
        and not host.startswith("-")
        and _HOST_RE.fullmatch(host) is not None
    )


def _ping_argv(host: str, count: int, timeout: float) -> list[str] | None:
    """Build the ping command line for the current platform, or None if unsupported.

    Linux -W is a per-reply wait in whole seconds. macOS -W is per reply in
    milliseconds. FreeBSD -t is an overall limit in seconds (count * timeout).
    Windows -w is per reply in milliseconds.
    """
    plat = sys.platform
    ipv6 = ":" in host
    if plat == "linux":
        return ["ping", "-c", str(count), "-W", str(max(1, math.ceil(timeout)))]
    if plat == "darwin":
        if ipv6:
            # macOS ping is IPv4 only; ping6 has no per-reply wait, so the
            # subprocess timeout bounds it.
            return ["ping6", "-c", str(count)]
        return ["ping", "-c", str(count), "-W", str(int(timeout * 1000))]
    if plat.startswith("freebsd"):
        return ["ping", "-c", str(count), "-t", str(max(1, math.ceil(timeout * count)))]
    if plat == "win32":
        return ["ping", *(["-6"] if ipv6 else []), "-n", str(count), "-w", str(int(timeout * 1000))]
    return None


def _parse_ping(output: str) -> tuple[int, int, float, float | None] | None:
    """Return (sent, received, loss_percent, avg_ms) from ping output, or None."""
    windows = _PING_WINDOWS_RE.search(output)
    if windows:
        sent, received = int(windows.group(1)), int(windows.group(2))
        loss = float(windows.group(3))
        avg_match = _PING_WINDOWS_AVG_RE.search(output)
        avg = float(avg_match.group(1)) if avg_match else None
        return sent, received, loss, avg

    summary = _PING_SUMMARY_RE.search(output)
    if not summary:
        return None
    sent, received = int(summary.group(1)), int(summary.group(2))
    loss = float(summary.group(3))
    avg_match = _PING_UNIX_AVG_RE.search(output)
    avg = float(avg_match.group(1)) if avg_match else None
    return sent, received, loss, avg


def _icmp(check: dict, timeout: float) -> dict:
    host = check.get("host")
    if not isinstance(host, str) or not host:
        raise _CheckError("missing host")
    if not _valid_host(host):
        return _result("icmp", host, False, "invalid host")

    count = check.get("count", ICMP_DEFAULT_COUNT)
    if not isinstance(count, int) or isinstance(count, bool):
        raise _CheckError("invalid count")
    count = min(max(count, 1), ICMP_MAX_COUNT)

    argv = _ping_argv(host, count, timeout)
    if argv is None:
        return _result("icmp", host, False, "ping is not supported on this platform")
    argv.append(host)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout * count + 2,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        return _result("icmp", host, False, "ping timed out")
    except FileNotFoundError:
        return _result("icmp", host, False, "ping is not installed")

    parsed = _parse_ping(proc.stdout or "")
    if parsed is None:
        return _result("icmp", host, False, "unexpected ping output")
    sent, received, loss, avg = parsed
    ok = received > 0
    if ok:
        detail = f"{received} of {sent} replies received"
    else:
        detail = "no replies received"
    return _result(
        "icmp",
        host,
        ok,
        detail,
        latency_s=(avg / 1000.0) if ok and avg is not None else None,
        loss_percent=loss,
        sent=sent,
        received=received,
    )


# ---------------------------------------------------------------- tcp


def _tcp(check: dict, timeout: float) -> dict:
    host = _str_field(check, "host")
    port = _port_field(check)
    start = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except TimeoutError:
        return _result("tcp", host, False, f"timed out after {_fmt_seconds(timeout)} s", port=port)
    except socket.gaierror:
        return _result("tcp", host, False, "host does not resolve", port=port)
    except ConnectionRefusedError:
        return _result("tcp", host, False, "connection refused", port=port)
    except OSError:
        return _result("tcp", host, False, "connection failed", port=port)
    elapsed = time.monotonic() - start
    sock.close()
    return _result("tcp", host, True, "port is open", latency_s=elapsed, port=port)


# ---------------------------------------------------------------- http / https


def _days_left(not_after: str, now: datetime) -> int:
    """Whole days from `now` until the getpeercert() notAfter time (negative once expired)."""
    expiry = datetime.strptime(not_after, _CERT_TIME_FORMAT).replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return math.floor((expiry - now).total_seconds() / 86400)


def _expiry_date(not_after: str) -> str:
    expiry = datetime.strptime(not_after, _CERT_TIME_FORMAT)
    return expiry.date().isoformat()


def _cert_problem(days: int, minimum: int) -> str | None:
    """The detail for a certificate that needs attention, or None when it is fine."""
    if days < 0:
        return "certificate has expired"
    if days < minimum:
        return f"certificate expires in {days} day{'s' if days != 1 else ''}"
    return None


def _tls_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if "self-signed" in text or "self signed" in text:
        return "self-signed certificate"
    if "expired" in text:
        return "certificate has expired"
    if "hostname" in text and "match" in text:
        return "hostname does not match certificate"
    if "unable to get local issuer" in text or "unknown ca" in text:
        return "issuer not trusted"
    if "verify failed" in text:
        return "certificate verification failed"
    return "handshake failed"


def _connect_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if "refused" in text:
        return "connection refused"
    if "resolution" in text or "getaddrinfo" in text or "name or service" in text:
        return "host does not resolve"
    return "connection failed"


def _peer_not_after(host: str, port: int, timeout: float) -> str:
    """Open a verified TLS connection and return the server certificate's notAfter.

    Uses the same CA bundle requests verified against (REQUESTS_CA_BUNDLE, else
    the bundle requests ships with), so the probe agrees with the request.
    """
    cafile = os.environ.get("REQUESTS_CA_BUNDLE") or _default_ca_bundle()
    ctx = ssl.create_default_context(cafile=cafile)
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert()
    not_after = cert.get("notAfter") if cert else None
    if not isinstance(not_after, str):
        raise ValueError("certificate has no notAfter")
    return not_after


def _http(check: dict, timeout: float) -> dict:
    url = _str_field(check, "url")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _CheckError("invalid URL") from None
    if parts.scheme not in ("http", "https"):
        return _result("http", url, False, "only http and https URLs are checked")
    if not parts.hostname:
        raise _CheckError("invalid URL")

    expect = _status_field(check)
    verify_tls = _bool_field(check, "verify_tls", True)
    min_days = _days_field(check)

    start = time.monotonic()
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            verify=verify_tls,
            stream=True,
        )
    except requests.exceptions.SSLError as exc:
        return _result("http", url, False, f"TLS: {_tls_reason(exc)}")
    except requests.exceptions.Timeout:
        return _result("http", url, False, f"timed out after {_fmt_seconds(timeout)} s")
    except requests.exceptions.ConnectionError as exc:
        return _result("http", url, False, _connect_reason(exc))
    except requests.exceptions.RequestException:
        return _result("http", url, False, "request failed")
    latency = time.monotonic() - start
    status = resp.status_code
    resp.close()  # the body is never read

    if expect is not None:
        status_ok = status in expect
    else:
        status_ok = 200 <= status <= 399
    status_detail = f"HTTP {status}"
    if not status_ok and expect is not None:
        status_detail += f", expected {', '.join(str(s) for s in expect)}"

    cert_days = None
    cert_expires = None
    cert_detail = None
    if parts.scheme == "https" and verify_tls:
        try:
            not_after = _peer_not_after(parts.hostname, parts.port or 443, timeout)
            cert_days = _days_left(not_after, datetime.now(timezone.utc))
            cert_expires = _expiry_date(not_after)
            cert_detail = _cert_problem(cert_days, min_days)
        except (OSError, ValueError, KeyError):
            cert_detail = "TLS: could not read certificate"

    if not status_ok:
        ok, detail = False, status_detail
    elif cert_detail is not None:
        ok, detail = False, cert_detail
    else:
        ok, detail = True, status_detail

    return _result(
        "http",
        url,
        ok,
        detail,
        latency_s=latency,
        status=status,
        cert_days_left=cert_days,
        cert_expires=cert_expires,
    )


# ---------------------------------------------------------------- dns


def _same_address(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def _dns(check: dict, timeout: float) -> dict:
    name = _str_field(check, "name")
    expect = _expect_field(check)

    # The stdlib resolver has no timeout, so the lookup runs in a worker thread.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    start = time.monotonic()
    try:
        future = pool.submit(socket.getaddrinfo, name, None)
        infos = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return _result("dns", name, False, f"timed out after {_fmt_seconds(timeout)} s")
    except socket.gaierror:
        return _result("dns", name, False, "name does not resolve")
    except OSError:
        return _result("dns", name, False, "lookup failed")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    latency = time.monotonic() - start

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return _result("dns", name, False, "name does not resolve", addresses=[])

    missing = [e for e in (expect or []) if not any(_same_address(e, a) for a in addresses)]
    if missing:
        return _result(
            "dns",
            name,
            False,
            f"resolved, but not to {missing[0]}",
            latency_s=latency,
            addresses=addresses,
        )
    count = len(addresses)
    noun = "address" if count == 1 else "addresses"
    return _result(
        "dns",
        name,
        True,
        f"resolved to {count} {noun}",
        latency_s=latency,
        addresses=addresses,
    )


# ---------------------------------------------------------------- snmp


def _community_field(check: dict) -> str:
    """The community the server handed inside the signed check list. Never logged."""
    value = check.get("community")
    if not isinstance(value, str) or not value:
        raise _CheckError("no SNMP community")
    return value


def _secret_field(check: dict, key: str, missing: str) -> str:
    """One of the SNMPv3 secrets handed inside the signed check list. Never logged."""
    value = check.get(key)
    if not isinstance(value, str) or not value:
        raise _CheckError(missing)
    return value


def _snmp_client(check: dict, host: str, port: int, timeout: float):
    """The client for this check: v3 when its params say so, v2c otherwise."""
    version = check.get("version", 2)
    if version == 3:
        return _snmp_v3_client(check, host, port, timeout)
    if version != 2:
        raise _CheckError("SNMP version must be 2 or 3")
    return snmp.V2cClient(
        host, community=_community_field(check), port=port, timeout=timeout, retries=1
    )


def _snmp_v3_client(check: dict, host: str, port: int, timeout: float):
    """A v3 client from the user, protocol and secrets inside the signed list."""
    auth = check.get("auth")
    if not isinstance(auth, str) or auth not in snmp.AUTH_PROTOCOLS:
        raise _CheckError("SNMPv3 auth must be sha or sha256")
    user = check.get("user")
    if not isinstance(user, str) or not user:
        raise _CheckError("no SNMPv3 user")

    priv = check.get("priv_password")
    return snmp.V3Client(
        host,
        user=user,
        auth_protocol=auth,
        auth_password=_secret_field(check, "auth_password", "no SNMPv3 auth password"),
        priv_password=None if priv is None else _secret_field(
            check, "priv_password", "invalid SNMPv3 privacy password"
        ),
        port=port,
        timeout=timeout,
        retries=1,
    )


def _snmp_oids(check: dict) -> list[tuple[str, str]]:
    raw = check.get("oids", [])
    if not isinstance(raw, list):
        raise _CheckError("invalid oids")
    oids: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("oid"), str)
            or not isinstance(item.get("label"), str)
        ):
            raise _CheckError("invalid oids")
        oids.append((item["oid"], item["label"]))
    if len(oids) > MAX_SNMP_OIDS:
        raise _CheckError("too many oids")
    return oids


def _snmp_value(tag: int, value: object) -> int | str | None:
    """One varbind as a result value.

    The tag is the SNMP type the agent used and says what the decoded bytes are:
    an int for INTEGER, Counter32, Gauge32, TimeTicks and Counter64, the dotted
    quad an IpAddress decodes to, a short string for an OCTET STRING, None for
    anything else.
    """
    if tag in (snmp.INTEGER, snmp.Counter32, snmp.Gauge32, snmp.TimeTicks, snmp.Counter64):
        return value if isinstance(value, int) else None
    if tag == snmp.IpAddress:
        return value if isinstance(value, str) else None
    if tag == snmp.OCTET_STRING and isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:MAX_SNMP_VALUE_CHARS]
    return None


def _uptime_phrase(seconds: int | None) -> str:
    """The uptime part of a detail: "" when there is nothing to say."""
    if seconds is None:
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _walk_indexed(client, root: str) -> dict[int, tuple[int, object]]:
    """One walk as ifIndex -> (tag, value). The ifIndex is the OID's last part."""
    rows: dict[int, tuple[int, object]] = {}
    for oid, tag, value in client.walk(root, max_rows=MAX_INTERFACES):
        try:
            index = int(oid.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            continue
        rows.setdefault(index, (tag, value))
    return rows


def _int_value(row) -> int | None:
    """The int an INTEGER, Gauge32 or Counter64 answers with; anything else is no value."""
    if row is None:
        return None
    value = _snmp_value(*row)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _interface_name(row) -> str:
    """ifName as text; an interface the device names nothing has no name."""
    if row is None:
        return ""
    value = _snmp_value(*row)
    return value if isinstance(value, str) else ""


def _snmp_interfaces(client) -> list[dict]:
    """The interface table, five walks joined on ifIndex.

    The ifName walk picks which interfaces exist: an index that only another
    column reports has no name to show it under. Counters go out as the device
    reported them — rates are worked out later, on read.
    """
    named = _walk_indexed(client, IF_NAME_OID)
    oper = _walk_indexed(client, IF_OPER_OID)
    speed = _walk_indexed(client, IF_HIGH_SPEED_OID)
    octets_in = _walk_indexed(client, IF_HC_IN_OID)
    octets_out = _walk_indexed(client, IF_HC_OUT_OID)
    return [{"index": index,
             "name": _interface_name(named[index]),
             "up": {1: True, 2: False}.get(_int_value(oper.get(index))),
             "speed_mbps": _int_value(speed.get(index)),
             "in_octets": _int_value(octets_in.get(index)),
             "out_octets": _int_value(octets_out.get(index))}
            for index in sorted(named)[:MAX_INTERFACES]]


def _ssh_fields(check: dict) -> tuple[str, int, str, str, str | None, str]:
    """(host, port, username, command, host_key, private_key), raising _CheckError on anything odd.

    The private key is validated here and passed along by value: it never goes
    into a result, an extra or a log line.
    """
    host = _str_field(check, "host")
    if not _valid_host(host):
        raise _CheckError("invalid host")
    port = _port_field(check) if "port" in check else 22

    username = check.get("username")
    if not isinstance(username, str) or not _SSH_USERNAME_RE.fullmatch(username):
        raise _CheckError("invalid username")

    command = check.get("command")
    # over-long is "missing" too: the argv element limit is 128 KiB, and a
    # backup command longer than 500 characters is not a command anyone wrote
    if (not isinstance(command, str) or not 1 <= len(command) <= MAX_SSH_COMMAND_CHARS
            or any(char in command for char in _SSH_COMMAND_FORBIDDEN)):
        raise _CheckError("invalid command")

    private_key = check.get("private_key")
    if (not isinstance(private_key, str) or not private_key.startswith(_SSH_KEY_HEADER)
            or len(private_key.encode("utf-8", "replace")) > _SSH_MAX_KEY_BYTES):
        raise _CheckError("invalid key")

    host_key = check.get("host_key")
    if isinstance(host_key, str) and not host_key:
        host_key = None  # an empty string is "not given", exactly like a missing key
    if host_key is None:
        return host, port, username, command, None, private_key
    if isinstance(host_key, str):
        parts = host_key.split(" ")
        if (len(parts) == 2 and parts[0] in _SSH_HOST_KEY_TYPES
                and _SSH_B64_RE.fullmatch(parts[1])):
            return host, port, username, command, host_key, private_key
    raise _CheckError("invalid host key")


def _ssh_known_hosts_field(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _ssh_argv(key_path: str, known_hosts: str, host: str, port: int, username: str,
              command: str, host_key: str | None, timeout: float) -> list[str]:
    strict = "yes" if host_key else "accept-new"
    return [
        "ssh",
        "-F", os.devnull,
        "-T",
        "-p", str(port),
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        # Never a key from an ssh-agent the poller's environment points at: the
        # check logs in with its own key or not at all.
        "-o", "IdentityAgent=none",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", f"GlobalKnownHostsFile={os.devnull}",
        "-o", f"StrictHostKeyChecking={strict}",
        "-o", "LogLevel=ERROR",
        "--",
        f"{username}@{host}",
        command,
    ]


def _ssh_key_bytes(private_key: str) -> bytes:
    """The key file's exact content: the given text, ending with one newline."""
    data = private_key.encode("utf-8", "replace")
    if not data.endswith(b"\n"):
        data += b"\n"
    return data


def _ssh_write_key(path: str, private_key: str) -> None:
    """The key file: O_EXCL so it can never overwrite anything, 0600 from the start."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(_ssh_key_bytes(private_key))


def _ssh_known_hosts_text(host_field: str, host_key: str | None) -> str:
    return f"{host_field} {host_key}\n" if host_key else ""


def _ssh_learned_host_key(path: str) -> str | None:
    """The key ssh accepted and wrote to known_hosts: "<type> <base64>", or None.

    Read on a failure too, so the server learns the key even when the login was
    refused for another reason.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline()
    except OSError:
        return None
    fields = line.split()
    if len(fields) < 3:
        return None
    return f"{fields[1]} {fields[2]}"


def _ssh_failure_detail(stderr: str, code: int) -> str:
    # the markers above are upper-cased; folding here keeps the match locale-proof
    folded = stderr.upper()
    if any(marker in folded for marker in _SSH_KEY_CHANGED_MARKERS):
        return "host key changed"
    for line in reversed(stderr.splitlines()):
        if line.strip():
            return line.strip()[:200]
    return f"ssh exited {code}"


_KNOWN_HOSTS_NAME = "known_hosts"


def _ssh_streams(completed: object) -> tuple[bytes, bytes]:
    """(stdout, stderr) as bytes, whatever the mock or the OS handed back."""
    out = getattr(completed, "stdout", b"") or b""
    err = getattr(completed, "stderr", b"") or b""
    if isinstance(out, str):
        out = out.encode("utf-8", "replace")
    if isinstance(err, str):
        err = err.encode("utf-8", "replace")
    return out, err


def _ssh_host_key(given: str | None, known_hosts_path: str) -> str | None:
    """The key to report: the one the dashboard gave, else the one ssh accepted."""
    return given or _ssh_learned_host_key(known_hosts_path)


def _ssh_failure(code: int, stderr: bytes, host: str, host_key: str | None,
                 known_hosts: str, latency: float) -> dict:
    """A refused login is a result too, and it still carries the host key."""
    return _result("ssh", host, False,
                   _ssh_failure_detail(stderr.decode("utf-8", "replace"), code), latency,
                   host_key=_ssh_host_key(host_key, known_hosts))


SSH_MAX_STDERR = 64_000  # ssh's own complaints; a device that floods stderr is cut off too


def _run_bounded(argv: list[str], *, timeout: float, env: dict,
                 max_stdout: int = SSH_MAX_OUTPUT, max_stderr: int = SSH_MAX_STDERR):
    """subprocess.run, except no stream is held past its cap.

    A device can print without end; with capture_output the whole of it would
    sit in the poller's memory before any length check. Here each stream is
    read in chunks on its own thread, and the moment one passes its cap the
    process is killed, so at most cap + one chunk is ever held. The answer is
    a CompletedProcess whose stdout is longer than ``max_stdout`` exactly when
    the cap was hit; ``subprocess.TimeoutExpired`` past ``timeout``.
    """
    import threading

    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    buffers = {"out": bytearray(), "err": bytearray()}

    def pump(stream, key, cap):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            room = cap + 1 - len(buffers[key])
            buffers[key] += chunk[:max(room, 0)]
            if len(buffers[key]) > cap:
                proc.kill()
                return

    readers = [threading.Thread(target=pump, args=(proc.stdout, "out", max_stdout), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err", max_stderr), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    return subprocess.CompletedProcess(argv, code, bytes(buffers["out"]), bytes(buffers["err"]))


def _ssh_run(directory: str, private_key: str, host: str, port: int, username: str,
             command: str, host_key: str | None, timeout: float) -> dict:
    """The temp-directory body of _ssh: the key file, the run, and its result."""
    key_path = os.path.join(directory, "key")
    known_hosts = os.path.join(directory, _KNOWN_HOSTS_NAME)
    _ssh_write_key(key_path, private_key)
    with open(known_hosts, "w", encoding="utf-8") as handle:
        handle.write(_ssh_known_hosts_text(_ssh_known_hosts_field(host, port), host_key))
    argv = _ssh_argv(key_path, known_hosts, host, port, username, command, host_key, timeout)
    started = time.monotonic()
    try:
        completed = _run_bounded(argv, timeout=SSH_TIMEOUT, env=clean_env())
    except subprocess.TimeoutExpired:
        return _result("ssh", host, False, "ssh timed out", time.monotonic() - started,
                       host_key=_ssh_host_key(host_key, known_hosts))
    except OSError as exc:
        return _result("ssh", host, False, f"could not run ssh: {exc.strerror or exc}"[:200])
    latency = time.monotonic() - started
    stdout, stderr = _ssh_streams(completed)
    raw_code = getattr(completed, "returncode", 1)
    code = raw_code if isinstance(raw_code, int) else 1

    # Before the exit code: _run_bounded kills a run that passed the cap, so its
    # code says "killed" while the real answer is "too much output".
    if len(stdout) > SSH_MAX_OUTPUT:
        return _result("ssh", host, False, "output over 1 MB", latency,
                       host_key=_ssh_host_key(host_key, known_hosts))
    if code != 0:
        return _ssh_failure(code, stderr, host, host_key, known_hosts, latency)

    text = stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    encoded = text.encode("utf-8")
    return _result("ssh", host, True, f"{len(text.splitlines())} lines", latency,
                   host_key=_ssh_host_key(host_key, known_hosts),
                   sha256=hashlib.sha256(encoded).hexdigest(), size=len(encoded),
                   output=text)


def _ssh(check: dict, timeout: float) -> dict:
    """One read-only command on a device. The ssh run itself may take SSH_TIMEOUT,
    not the check's own timeout — key auth plus a slow CLI is slower than a ping."""
    host, port, username, command, host_key, private_key = _ssh_fields(check)
    if shutil.which("ssh") is None:
        return _result("ssh", host, False, "no ssh client on this poller")

    directory = tempfile.mkdtemp(prefix="vigil-ssh-")
    try:
        return _ssh_run(directory, private_key, host, port, username, command, host_key, timeout)
    finally:
        # the private key must not survive the check, whatever happened above
        shutil.rmtree(directory, ignore_errors=True)


def _snmp(check: dict, timeout: float) -> dict:
    host = _str_field(check, "host")
    if not _valid_host(host):
        raise _CheckError("invalid host")
    port = 161 if check.get("port") is None else _port_field(check)
    labelled = _snmp_oids(check)
    oids = [SYSNAME_OID, SYSUPTIME_OID] + [oid for oid, _label in labelled]
    client = _snmp_client(check, host, port, timeout)
    start = time.monotonic()
    try:
        values = client.get(oids)
    except snmp.SnmpTimeout:
        # A wrong community and a closed UDP port look exactly the same from here.
        return _result("snmp", host, False, "no SNMP response")
    except snmp.SnmpError as exc:
        return _result("snmp", host, False, str(exc)[:200])
    latency = time.monotonic() - start

    sys_name = values.get(SYSNAME_OID)
    sys_name_text = (
        sys_name[1].decode("utf-8", errors="replace")[:MAX_SNMP_VALUE_CHARS]
        if isinstance(sys_name, tuple) and sys_name[0] == snmp.OCTET_STRING
        and isinstance(sys_name[1], bytes) else ""
    )
    uptime = values.get(SYSUPTIME_OID)
    uptime_s = uptime[1] // 100 if isinstance(uptime, tuple) and isinstance(uptime[1], int) else None

    extras: dict = {"sys_name": sys_name_text or None, "uptime_s": uptime_s}
    missing: list[str] = []
    for oid, label in labelled:
        entry = values.get(oid)
        if entry is None or entry[0] in (snmp.noSuchObject, snmp.noSuchInstance):
            missing.append(label)
            extras[label] = None
            continue
        extras[label] = _snmp_value(entry[0], entry[1])

    detail = "up"
    uptime_part = _uptime_phrase(uptime_s)
    if uptime_part:
        detail += f" {uptime_part}"
    if sys_name_text:
        detail += f", {sys_name_text}"
    if missing:
        return _result("snmp", host, False, "no such object: " + ", ".join(missing),
                       latency_s=latency, **extras)
    if check.get("interfaces") is not True:
        return _result("snmp", host, True, detail[:200], latency_s=latency, **extras)
    # The interface table is a second exchange on the same client. An agent
    # that cannot answer it leaves the GET's result exactly as it was, with
    # one clause added and no interfaces key at all.
    try:
        interfaces = _snmp_interfaces(client)
    except (snmp.SnmpTimeout, snmp.SnmpError):
        return _result("snmp", host, True, f"{detail}, interfaces unreadable"[:200],
                       latency_s=latency, **extras)
    return _result("snmp", host, True, detail[:200], latency_s=latency,
                   interfaces=interfaces, **extras)


# ---------------------------------------------------------------- entry point

_HANDLERS = {"icmp": _icmp, "tcp": _tcp, "http": _http, "dns": _dns, "snmp": _snmp, "ssh": _ssh}
_TARGET_KEYS = {"icmp": "host", "tcp": "host", "http": "url", "dns": "name", "snmp": "host",
                "ssh": "host"}


def run_check(check: dict, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run one check. Never raises; failures come back as ok False results."""
    if not isinstance(check, dict):
        return _result("", "", False, "unsupported check")
    kind = check.get("kind")
    name = kind if isinstance(kind, str) else ""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _result(name, "", False, "unsupported check")

    key = _TARGET_KEYS[name]
    target_value = check.get(key)
    target = target_value if isinstance(target_value, str) else ""
    try:
        return handler(check, _clamp_timeout(timeout))
    except _CheckError as exc:
        return _result(name, target, False, str(exc))
    except Exception:  # noqa: BLE001 - the contract is that run_check never raises
        return _result(name, target, False, "check failed unexpectedly")
