"""Refuse to fetch destinations that exist to be reached from the inside.

Vigil's server can reach hosts an operator's workstation cannot, so fetching a
URL on their behalf turns Vigil into a proxy into its own network. The answer
is not to block everything internal — an internal mirror is a legitimate and
common source, and the page warns about non-catalog URLs rather than refusing
them. It is to block the small set of destinations with no legitimate ISO on
them, of which cloud instance metadata is the one that matters: on a
cloud-hosted Vigil it hands out credentials for the entire account.

The check runs on the RESOLVED address, not the URL string. A hostname pointed
at a blocked address is blocked, and the caller re-checks after every redirect
— a guard that only reads the string the operator typed is not a guard. That
also covers non-dotted IPv4 literals (decimal, hex, octal): they are not
special-cased here, they simply resolve to the same address a dotted-quad
would and fall into the same check.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: Destinations refused outright, with no override.
BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local, incl. AWS/Azure metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDSv6
)

#: Names that resolve to a metadata service on some providers even when the
#: address does not fall in a blocked range.
BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

_ALLOWED_SCHEMES = ("https", "http")


def _resolve(host: str) -> list[str]:
    """Every address this name resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def _is_blocked(addr) -> bool:
    """True if `addr` (an ipaddress.IPv4Address/IPv6Address) falls in a
    blocked network -- checking both the address as given and, for an
    IPv4-mapped IPv6 address (``::ffff:a.b.c.d``), the embedded IPv4 form.
    Without the second check, ``::ffff:169.254.169.254`` would sail past a
    check that only ever compares IPv6 addresses to the IPv6 entries in
    BLOCKED_NETWORKS."""
    candidates = [addr]
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    for candidate in candidates:
        for net in BLOCKED_NETWORKS:
            if candidate.version == net.version and candidate in net:
                return True
    return False


def check_url(url, allow_http: bool = False) -> str:
    """Return "" if Vigil may fetch this URL, else a sentence saying why not.

    Never raises: callers run this before doing anything else, so a traceback
    here would become a 500 on a safety check.
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "That does not parse as a URL."

    if parts.scheme not in _ALLOWED_SCHEMES:
        return (f"Only https:// (or http:// with the override) can be "
                f"fetched — {parts.scheme or 'no scheme'} is not supported.")
    if parts.scheme == "http" and not allow_http:
        return ("Refusing to fetch over plain HTTP. The checksum is what "
                "protects the bytes, but the URL itself is worth protecting "
                "too — use https://, or tick the override for an internal "
                "mirror that has no certificate.")

    try:
        host = parts.hostname
    except ValueError:
        return "That URL has no host in it."
    if not host:
        return "That URL has no host in it."

    if host.lower() in BLOCKED_HOSTNAMES:
        return (f"Refusing to fetch from {host} — that is a cloud instance "
                f"metadata service, not an image source. On a cloud-hosted "
                f"Vigil it returns credentials for the whole account.")

    try:
        addresses = _resolve(host)
    except OSError as exc:
        return f"Could not resolve {host}: {exc}"

    for raw in addresses:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_blocked(addr):
            return (
                f"Refusing to fetch from {host} — it resolves to {addr}, "
                f"which is loopback or link-local. Link-local covers cloud "
                f"instance metadata, which returns credentials for the "
                f"whole account rather than an ISO. Vigil will not fetch "
                f"from there on your behalf.")
    return ""


def is_catalog_url(url: str) -> bool:
    """True when this URL is one Vigil ships in its own catalog."""
    from .catalog import CATALOG

    return any(entry["url"] == url for entry in CATALOG)
