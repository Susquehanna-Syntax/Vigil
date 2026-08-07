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
would and fall into the same check. It also covers "this host" spellings that
are not loopback's usual dotted form: 0.0.0.0 and IPv6 :: are the unspecified
address, not "nowhere" — on Linux, connect() to either lands on whatever is
bound to loopback, so both are blocked alongside 127.0.0.0/8 and ::1. And it
covers an IPv4 address written as an IPv6 literal, in both the current
"IPv4-mapped" form (::ffff:a.b.c.d) and the older "IPv4-compatible" form
(::a.b.c.d) — see `_embedded_ipv4`.
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
    # "Unspecified" is not "nowhere": on Linux, connect() to 0.0.0.0 (or its
    # IPv6 counterpart ::) is treated as 127.0.0.1/::1 -- so it reaches
    # whatever is bound to loopback exactly like the literal loopback address
    # does, and needs the same refusal.
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud instance metadata
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


def _embedded_ipv4(addr):
    """The IPv4 address written inside an IPv6 literal, or None.

    Two IPv6 notations let an operator spell an IPv4 address as an IPv6
    literal, and the kernel accepts a connect() to either: "IPv4-mapped"
    (``::ffff:a.b.c.d``, exposed by stdlib as `.ipv4_mapped`) and the older,
    deprecated "IPv4-compatible" form (``::a.b.c.d``, top 96 bits zero,
    which `.ipv4_mapped` does NOT recognise -- it only matches the
    ``::ffff:`` prefix). Both are handled here so neither can be used to
    walk a blocked IPv4 address past a check that only ever compares
    IPv6 addresses to the IPv6 entries in BLOCKED_NETWORKS.

    This also flags a handful of native low-numbered IPv6 addresses (``::5``
    etc.) as if they carried an embedded IPv4 address -- harmless, since the
    result is only ever compared against the IPv4 entries in
    BLOCKED_NETWORKS, so it can only turn into a refusal when those low bits
    also happen to be a blocked IPv4 address.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    if getattr(addr, "version", None) == 6:
        packed = addr.packed
        if packed[:12] == b"\x00" * 12:
            return ipaddress.IPv4Address(packed[12:])
    return None


def _is_blocked(addr) -> bool:
    """True if `addr` (an ipaddress.IPv4Address/IPv6Address) falls in a
    blocked network -- checking both the address as given and, for an IPv6
    address with an embedded IPv4 form (mapped or compatible), that embedded
    address. Without the second check, an IPv6 spelling of a blocked IPv4
    address would sail past a check that only ever compares IPv6 addresses
    to the IPv6 entries in BLOCKED_NETWORKS."""
    candidates = [addr]
    embedded = _embedded_ipv4(addr)
    if embedded is not None:
        candidates.append(embedded)
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

    if host.lower().rstrip(".") in BLOCKED_HOSTNAMES:
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
                f"which is loopback, unspecified, link-local, or a known "
                f"cloud metadata address. Those exist to be reached from "
                f"inside this host or account, not fetched on an "
                f"operator's behalf — link-local in particular covers "
                f"cloud instance metadata, which returns credentials for "
                f"the whole account rather than an ISO. Vigil will not "
                f"fetch from there on your behalf.")
    return ""


def is_catalog_url(url: str) -> bool:
    """True when this URL is one Vigil ships in its own catalog."""
    from .catalog import CATALOG

    return any(entry["url"] == url for entry in CATALOG)
