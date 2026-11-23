"""Metrics are pruned on every kind of database (sprint M4): a plain table by
DELETE, a hypertable with a retention policy by that policy, and a hypertable
without one (an Apache-licensed TimescaleDB, which has no policies) by
drop_chunks, which that build does have."""
from unittest import mock

from django.test import TestCase, override_settings

from apps.alerts import tasks


class PruneByDatabaseKindTests(TestCase):
    @override_settings(VIGIL_METRIC_RETENTION_DAYS=30)
    def test_a_hypertable_without_a_policy_drops_its_old_chunks(self):
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [("_hyper_1_1_chunk",), ("_hyper_1_2_chunk",)]
        with mock.patch.object(tasks, "_metricpoint_is_hypertable", return_value=True), \
                mock.patch.object(tasks, "_metricpoint_has_retention_policy", return_value=False), \
                mock.patch("django.db.connection.cursor", return_value=cursor):
            out = tasks.prune_old_metric_points()
        sql, params = cursor.execute.call_args.args
        self.assertIn("drop_chunks('metrics_metricpoint'", sql)
        self.assertEqual(params[0].days, 30)
        self.assertIn("Dropped 2 metric chunks", out)

    def test_a_hypertable_with_a_policy_is_left_to_it(self):
        with mock.patch.object(tasks, "_metricpoint_is_hypertable", return_value=True), \
                mock.patch.object(tasks, "_metricpoint_has_retention_policy", return_value=True), \
                mock.patch("django.db.connection.cursor") as cursor:
            out = tasks.prune_old_metric_points()
        # Device check results are still pruned (an ordinary table); what must
        # not happen is a drop_chunks of the metrics hypertable.
        self.assertFalse(any("drop_chunks" in str(c) for c in cursor.mock_calls))
        self.assertIn("drop_chunks policy", out)
