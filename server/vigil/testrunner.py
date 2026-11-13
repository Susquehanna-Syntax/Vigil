"""The test runner: strict site scoping, test bodies in a system scope.

Strict mode makes a scoped query with no scope raise ScopeMissing, so an
undeclared background path fails a test instead of quietly reading nothing.
Test bodies themselves are trusted code that sets up and inspects every
site's rows, so they run in a system scope; a request made through the test
client still gets its user's scope from SiteScopeMiddleware, and
``scoping.no_scope_for_tests()`` clears it where a test needs no scope.
"""
from django.conf import settings
from django.db import connections
from django.test.runner import DiscoverRunner

from vigil import scoping


class ScopedTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.VIGIL_SCOPE_STRICT = True
        self._vigil_scope = scoping.system(reason="test body: tests are trusted code")
        self._vigil_scope.__enter__()

    def teardown_test_environment(self, **kwargs):
        self._vigil_scope.__exit__(None, None, None)
        super().teardown_test_environment(**kwargs)

    def teardown_databases(self, old_config, **kwargs):
        # A thread from an async test (the remote-session relays run their
        # database calls in worker threads) can leave a connection open, and
        # Postgres refuses to drop a database anyone is still in. Every test
        # has finished, so those sessions are only in the way.
        for conn in connections.all(initialized_only=True):
            if conn.vendor != "postgresql":
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        super().teardown_databases(old_config, **kwargs)
