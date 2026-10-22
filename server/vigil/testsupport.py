"""For tests that need the Business apps (the vigil-business package).

A source checkout of Vigil runs without that package, so the tests that
exercise core together with Business skip there and run wherever it is
installed: the official CI and the Business repo's own CI.
"""
import unittest

from django.conf import settings

#: Skip one test (or class) when Business is not installed.
requires_business = unittest.skipUnless(
    settings.BUSINESS_INSTALLED, "needs the vigil-business package")


def require_business_module():
    """Call at the top of a test module, before its Business imports: skips
    the whole module when Business is not installed."""
    if not settings.BUSINESS_INSTALLED:
        raise unittest.SkipTest("needs the vigil-business package")
