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


class PicklableAsyncioTestCase(__import__("unittest").IsolatedAsyncioTestCase):
    """IsolatedAsyncioTestCase that ``manage.py test --parallel`` can ship to a
    worker. Python 3.13 keeps a ``contextvars.Context`` on every instance
    (``_asyncioTestContext``), which cannot be pickled, so the parallel
    runner failed outright. The context is dropped on the way out and a fresh
    copy made on the way in, which is what __init__ would have done."""

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_asyncioTestContext", None)
        return state

    def __setstate__(self, state):
        import contextvars
        self.__dict__.update(state)
        self._asyncioTestContext = contextvars.copy_context()
