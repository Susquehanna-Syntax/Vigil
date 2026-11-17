"""Basic checks for devices that cannot run an agent.

run_check() takes one check description (a dict with a "kind") and returns a
plain result dict. It never raises: every failure, including a malformed
check, becomes a result with ok False and a short detail sentence.

Kinds: "icmp" (system ping), "tcp" (connect), "http" (status, plus TLS
certificate expiry for https), "dns" (name resolution), "snmp" (SNMP v2c GET).
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import math
import os
import re
import socket
import ssl
import subprocess
import sys
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

_CERT_TIME_FORMAT = "%b %d %H:%M:%S %Y %Z"  # getpeercert() notAfter, e.g. "Jun  1 12:00:00 2027 GMT"
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")

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


def _snmp(check: dict, timeout: float) -> dict:
    host = _str_field(check, "host")
    if not _valid_host(host):
        raise _CheckError("invalid host")
    port = 161 if check.get("port") is None else _port_field(check)
    community = _community_field(check)
    labelled = _snmp_oids(check)
    oids = [SYSNAME_OID, SYSUPTIME_OID] + [oid for oid, _label in labelled]
    client = snmp.V2cClient(host, community=community, port=port, timeout=timeout, retries=1)
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
    return _result("snmp", host, True, detail[:200], latency_s=latency, **extras)


# ---------------------------------------------------------------- entry point

_HANDLERS = {"icmp": _icmp, "tcp": _tcp, "http": _http, "dns": _dns, "snmp": _snmp}
_TARGET_KEYS = {"icmp": "host", "tcp": "host", "http": "url", "dns": "name", "snmp": "host"}


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
