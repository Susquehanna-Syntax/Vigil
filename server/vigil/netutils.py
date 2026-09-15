"""Is this address on a network the operator plausibly controls?

Vigil is built for homelabs, so "plain HTTP" is not automatically a mistake —
a LAN address reached over a switch the operator owns is a different risk from
a public hostname reached over the internet. Two places need that distinction
and had been answering it separately: the rebuild ceremony's transport gate,
and the Civil SSO key fetch.

Kept deliberately conservative: anything not recognisably private is treated as
public, because the failure direction matters. Calling a public address private
downgrades a security decision silently; calling a private one public asks an
operator to acknowledge something they already knew.
"""

from __future__ import annotations

import ipaddress

#: Suffixes reserved for local networks by convention or by RFC 6762/8375.
PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".home", ".arpa", ".home.arpa")


def is_private_hostname(hostname: str) -> bool:
    """True when *hostname* names a host on a local network.

    Accepts a bare hostname, an IPv4 or IPv6 literal, or an IPv6-mapped IPv4
    address. Does not resolve DNS: resolution would make the answer depend on
    the network at call time, and an attacker who controls a name could point
    it at a private address to win a check.
    """
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        return True

    # A bracketed IPv6 literal arrives as [::1] from a URL netloc.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A dotted public name, or something that is not an address at all.
        return False

    # An IPv6-mapped IPv4 address (::ffff:127.0.0.1) is that IPv4 address for
    # every purpose that matters here, and is a classic way to dodge a check
    # that only looks at the textual form.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped

    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    )
