"""The advisory lock the periodic tasks serialize on."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from vigil.locks import _key, advisory_lock


class AdvisoryLockTests(SimpleTestCase):

    def test_the_key_is_stable_and_fits_a_bigint(self):
        """pg_try_advisory_lock takes a signed 64-bit integer; a name that
        hashed differently between workers would lock nothing."""
        first = _key("alerts.evaluate_alert_rules")
        self.assertEqual(first, _key("alerts.evaluate_alert_rules"))
        self.assertNotEqual(first, _key("playbooks.reconcile"))
        self.assertGreaterEqual(first, 0)
        self.assertLess(first, 2 ** 63)

    def test_sqlite_always_grants_it(self):
        """Local dev and the suite have no concurrent workers to protect
        against, and SQLite has no advisory locks to use."""
        with patch("vigil.locks.connection") as conn:
            conn.vendor = "sqlite"
            with advisory_lock("anything") as acquired:
                self.assertTrue(acquired)

    def _pg_connection(self, granted):
        conn = MagicMock()
        conn.vendor = "postgresql"
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (granted,)
        return conn, cursor

    def test_a_held_lock_is_reported_as_not_acquired(self):
        """The caller skips rather than waits: whoever holds it is already
        doing the same sweep."""
        conn, _ = self._pg_connection(False)
        with patch("vigil.locks.connection", conn):
            with advisory_lock("alerts.evaluate_alert_rules") as acquired:
                self.assertFalse(acquired)

    def test_the_lock_is_released_even_when_the_body_raises(self):
        """A beat that dies holding the lock would wedge every later pass."""
        conn, cursor = self._pg_connection(True)
        with patch("vigil.locks.connection", conn):
            with self.assertRaises(RuntimeError):
                with advisory_lock("playbooks.reconcile") as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError("beat exploded")

        statements = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(any("pg_advisory_unlock" in s for s in statements),
                        "the lock was not released after an exception")

    def test_a_lock_that_was_never_acquired_is_not_released(self):
        """Unlocking someone else's lock is worse than never taking one."""
        conn, cursor = self._pg_connection(False)
        with patch("vigil.locks.connection", conn):
            with advisory_lock("playbooks.reconcile"):
                pass
        statements = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertFalse(any("pg_advisory_unlock" in s for s in statements))
