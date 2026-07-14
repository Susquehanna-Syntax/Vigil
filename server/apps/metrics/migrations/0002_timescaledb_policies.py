"""Convert MetricPoint into a TimescaleDB hypertable and bake in the storage
policies that keep ``vigil_pgdata`` bounded on a fresh deployment.

Why this migration exists
-------------------------
``MetricPoint`` was created as a *plain* Postgres table (see 0001) even though
it was "designed for" hypertable conversion. A plain table cannot use native
columnar compression or ``drop_chunks`` retention, so every sample was retained
uncompressed and fully indexed, and the row-by-row DELETE prune never returned
disk to the OS. That is what let ``vigil_pgdata`` grow to 26 GB and fill the
host. This migration fixes it at the schema level so no operator has to
remember a manual step on a new instance.

What it does (Postgres + TimescaleDB only; SQLite/other vendors are skipped)
    1. Convert ``metrics_metricpoint`` to a hypertable partitioned on ``time``.
    2. Enable columnar compression (segment by series, order by time) and add a
       policy to compress chunks older than VIGIL_TS_COMPRESS_AFTER.
    3. Add a retention policy dropping raw chunks older than
       VIGIL_METRIC_RETENTION_DAYS (drop_chunks frees disk immediately, unlike
       the old DELETE).
    4. Create continuous aggregates (1-hour and 1-day rollups per series) with
       their own longer retention so downsampled trend history survives raw
       expiry.

Every statement is idempotent (IF NOT EXISTS / if_not_exists) so the recovery
runbook can pre-convert the fat table on a near-full disk and then run migrate
without conflict. ``atomic = False`` because a continuous aggregate cannot be
created inside a transaction block.
"""
import os

from django.db import migrations

TABLE = "metrics_metricpoint"

# Tunables (read at apply time; env overridable for unusual data models).
CHUNK_INTERVAL = os.environ.get("VIGIL_TS_CHUNK_INTERVAL", "1 day")
COMPRESS_AFTER = os.environ.get("VIGIL_TS_COMPRESS_AFTER", "7 days")
RAW_RETENTION_DAYS = os.environ.get("VIGIL_METRIC_RETENTION_DAYS", "30")
HOURLY_RETENTION = os.environ.get("VIGIL_TS_HOURLY_RETENTION", "365 days")
DAILY_RETENTION = os.environ.get("VIGIL_TS_DAILY_RETENTION", "1825 days")


def apply(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != "postgresql":
        return  # SQLite / local dev — MetricPoint stays a plain table.

    with conn.cursor() as cur:
        # TimescaleDB must be present. The timescale/timescaledb image installs
        # it in POSTGRES_DB automatically; if a plain Postgres is in use we try
        # to create it, and if that is not permitted we leave the table plain
        # (the DELETE-based prune task remains the fallback).
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if not cur.fetchone():
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            except Exception:
                return

        # 1. Hypertable. Drop the surrogate-id PK first: TimescaleDB forbids a
        #    unique index that does not include the partitioning column. The
        #    ``id`` column and its sequence stay, so the Django ORM keeps
        #    addressing rows by pk exactly as before.
        cur.execute(
            "SELECT 1 FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = %s",
            [TABLE],
        )
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {TABLE}_pkey")
            cur.execute(
                f"SELECT create_hypertable('{TABLE}', 'time', "
                f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', "
                f"migrate_data => TRUE, if_not_exists => TRUE)"
            )

        # 2. Continuous aggregates — built before raw retention so downsampled
        #    history is materialized while raw chunks still exist. Grouping
        #    includes ``labels`` so per-interface / per-mount series stay
        #    distinct rather than being averaged together.
        cur.execute(
            f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS {TABLE}_hourly
            WITH (timescaledb.continuous) AS
            SELECT host_id, category, metric, labels,
                   time_bucket(INTERVAL '1 hour', time) AS bucket,
                   avg(value)        AS avg_value,
                   min(value)        AS min_value,
                   max(value)        AS max_value,
                   last(value, time) AS last_value,
                   count(*)          AS sample_count
            FROM {TABLE}
            GROUP BY host_id, category, metric, labels, bucket
            WITH NO DATA
            """
        )
        cur.execute(
            f"SELECT add_continuous_aggregate_policy('{TABLE}_hourly', "
            "start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour', "
            "schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE)"
        )
        cur.execute(
            f"SELECT add_retention_policy('{TABLE}_hourly', "
            f"INTERVAL '{HOURLY_RETENTION}', if_not_exists => TRUE)"
        )

        cur.execute(
            f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS {TABLE}_daily
            WITH (timescaledb.continuous) AS
            SELECT host_id, category, metric, labels,
                   time_bucket(INTERVAL '1 day', time) AS bucket,
                   avg(value)        AS avg_value,
                   min(value)        AS min_value,
                   max(value)        AS max_value,
                   last(value, time) AS last_value,
                   count(*)          AS sample_count
            FROM {TABLE}
            GROUP BY host_id, category, metric, labels, bucket
            WITH NO DATA
            """
        )
        cur.execute(
            f"SELECT add_continuous_aggregate_policy('{TABLE}_daily', "
            "start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 day', "
            "schedule_interval => INTERVAL '1 day', if_not_exists => TRUE)"
        )
        cur.execute(
            f"SELECT add_retention_policy('{TABLE}_daily', "
            f"INTERVAL '{DAILY_RETENTION}', if_not_exists => TRUE)"
        )

        # 3. Compression — the biggest single win (10-20x typical for metrics).
        cur.execute(
            f"ALTER TABLE {TABLE} SET ("
            "timescaledb.compress, "
            "timescaledb.compress_segmentby = 'host_id, category, metric', "
            "timescaledb.compress_orderby = 'time DESC')"
        )
        cur.execute(
            f"SELECT add_compression_policy('{TABLE}', "
            f"INTERVAL '{COMPRESS_AFTER}', if_not_exists => TRUE)"
        )

        # 4. Retention on raw — drop_chunks frees disk per chunk immediately.
        cur.execute(
            f"SELECT add_retention_policy('{TABLE}', "
            f"INTERVAL '{RAW_RETENTION_DAYS} days', if_not_exists => TRUE)"
        )


def revert(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != "postgresql":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if not cur.fetchone():
            return
        # Tear down policies + rollups. The hypertable itself is left in place;
        # reverting a populated hypertable back to a plain table is unsafe and
        # not something a downgrade should attempt automatically.
        for stmt in (
            f"SELECT remove_retention_policy('{TABLE}', if_exists => TRUE)",
            f"SELECT remove_compression_policy('{TABLE}', if_exists => TRUE)",
            f"DROP MATERIALIZED VIEW IF EXISTS {TABLE}_daily CASCADE",
            f"DROP MATERIALIZED VIEW IF EXISTS {TABLE}_hourly CASCADE",
            f"ALTER TABLE {TABLE} SET (timescaledb.compress = false)",
        ):
            try:
                cur.execute(stmt)
            except Exception:
                pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("metrics", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(apply, revert),
    ]
