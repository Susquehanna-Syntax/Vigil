"""Vigil's server sits inside the network and can reach what an operator's
workstation cannot. Fetching a URL on their behalf makes Vigil a proxy into its
own network, so a small, specific set of destinations is refused outright.

The blocked set is deliberately narrow. Private ranges are ALLOWED — pulling
from an internal mirror is a legitimate and common thing to want, and the page
warns rather than refuses. Only destinations with no legitimate ISO on them are
blocked, and cloud instance metadata is the one that matters: on a cloud-hosted
Vigil it hands out credentials for the whole account.
"""
from unittest.mock import patch

from django.test import TestCase

from .fetch_guard import check_url


def _resolves_to(addr):
    """Patch name resolution so tests never touch DNS."""
    return patch("apps.reprovision.fetch_guard._resolve",
                 lambda host: [addr])


class BlockedDestinationTests(TestCase):
    def test_aws_metadata_is_blocked(self):
        with _resolves_to("169.254.169.254"):
            self.assertNotEqual(check_url("https://169.254.169.254/latest/meta-data/"), "")

    def test_the_refusal_explains_why(self):
        with _resolves_to("169.254.169.254"):
            msg = check_url("https://169.254.169.254/x.iso").lower()
        self.assertIn("metadata", msg)

    def test_loopback_is_blocked(self):
        with _resolves_to("127.0.0.1"):
            self.assertNotEqual(check_url("https://127.0.0.1/x.iso"), "")

    def test_ipv6_loopback_is_blocked(self):
        with _resolves_to("::1"):
            self.assertNotEqual(check_url("https://[::1]/x.iso"), "")

    def test_link_local_is_blocked(self):
        with _resolves_to("169.254.10.5"):
            self.assertNotEqual(check_url("https://169.254.10.5/x.iso"), "")

    def test_a_hostname_resolving_to_metadata_is_blocked(self):
        """The check is on the resolved address, not the string — otherwise a
        DNS record pointed at metadata trivially bypasses it."""
        with _resolves_to("169.254.169.254"):
            self.assertNotEqual(check_url("https://mirror.example.com/x.iso"), "")


class AllowedDestinationTests(TestCase):
    def test_a_public_https_url_is_allowed(self):
        with _resolves_to("185.125.190.36"):
            self.assertEqual(check_url("https://releases.ubuntu.com/x.iso"), "")

    def test_a_private_range_mirror_is_allowed(self):
        """Deliberate: an internal mirror is a legitimate source. The page
        warns; the guard does not refuse."""
        with _resolves_to("192.168.1.50"):
            self.assertEqual(check_url("https://192.168.1.50/isos/x.iso"), "")

    def test_rfc1918_ten_net_is_allowed(self):
        with _resolves_to("10.0.0.5"):
            self.assertEqual(check_url("https://mirror.internal/x.iso"), "")


class SchemeTests(TestCase):
    def test_plain_http_is_refused_by_default(self):
        with _resolves_to("185.125.190.36"):
            self.assertNotEqual(check_url("http://releases.ubuntu.com/x.iso"), "")

    def test_plain_http_is_permitted_with_an_explicit_override(self):
        with _resolves_to("185.125.190.36"):
            self.assertEqual(
                check_url("http://mirror.internal/x.iso", allow_http=True), "")

    def test_a_non_http_scheme_is_refused(self):
        for url in ("file:///etc/passwd", "ftp://x/y.iso", "gopher://x/1"):
            self.assertNotEqual(check_url(url), "", url)


class MalformedInputTests(TestCase):
    def test_it_never_raises(self):
        for url in ("", "not a url", "https://", "://x", None):
            self.assertIsInstance(check_url(url), str)

    def test_unresolvable_host_is_refused_not_crashed(self):
        with patch("apps.reprovision.fetch_guard._resolve",
                   side_effect=OSError("no such host")):
            self.assertNotEqual(check_url("https://nope.invalid/x.iso"), "")


class BypassTests(TestCase):
    """Each of these covers a specific way a naive, string-only guard could
    be talked past. None of them are in the brief's list."""

    def test_non_dotted_decimal_ipv4_literal_is_blocked(self):
        """169.254.169.254 written as a plain decimal integer. This is not
        mocked: the point is to observe what the real resolver does with a
        numeric literal, which needs no network trip to resolve. Confirmed
        by hand (Python 3.12, glibc getaddrinfo): it normalises the decimal
        form straight to 169.254.169.254, so the guard blocks it for the
        same reason it blocks the dotted-quad form -- the resolved address
        lands in BLOCKED_NETWORKS regardless of how the operator spelled it."""
        self.assertNotEqual(check_url("http://2852039166/x.iso", allow_http=True), "")

    def test_hex_ipv4_literal_is_blocked(self):
        """Same address, hex form (0xA9FEA9FE). Also unmocked -- observed to
        normalise the same way as the decimal case above."""
        self.assertNotEqual(check_url("http://0xA9FEA9FE/x.iso", allow_http=True), "")

    def test_one_of_several_resolved_addresses_being_blocked_is_enough(self):
        """A name with both a public A record and a metadata A record must
        not slip through by only checking the first address returned."""
        with patch("apps.reprovision.fetch_guard._resolve",
                   lambda host: ["185.125.190.36", "169.254.169.254"]):
            self.assertNotEqual(check_url("https://multi.example.com/x.iso"), "")

    def test_ipv4_mapped_ipv6_form_of_metadata_is_blocked(self):
        """::ffff:169.254.169.254 is the IPv4-mapped IPv6 notation for the
        AWS/Azure metadata address. Observed: ipaddress.IPv6Address exposes
        the embedded address via `.ipv4_mapped`, and the implementation
        checks it against the IPv4 blocked networks too -- so this form is
        covered, not merely documented as a gap."""
        with _resolves_to("::ffff:169.254.169.254"):
            self.assertNotEqual(check_url("https://mapped.example.com/x.iso"), "")
