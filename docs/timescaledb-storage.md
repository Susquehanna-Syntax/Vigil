# TimescaleDB storage — compression, retention & disk recovery

## Why this exists

`MetricPoint` (`metrics_metricpoint`) was originally created as a **plain
Postgres table**, never a TimescaleDB hypertable. A plain table cannot use
native columnar compression or `drop_chunks` retention, so:

- every sample was stored **uncompressed** and carried ~5 indexes;
- the hourly `metrics.prune_old_metric_points` task used row-by-row `DELETE`,
  which marks tuples dead but **never returns disk to the OS** (no
  `VACUUM FULL`), leaving the file at its high-water mark plus index bloat.

That is how `vigil_pgdata` reached 26 GB and filled the host.

## The fix (schema default)

Migration `apps/metrics/migrations/0002_timescaledb_policies.py` runs
automatically on Postgres+TimescaleDB (skipped on SQLite / plain Postgres) and:

1. **Hypertable** — converts `metrics_metricpoint`, partitioned on `time`,
   1-day chunks. The surrogate `id` PK is dropped (TimescaleDB forbids a unique
   index without the partition column); the `id` column + sequence remain, so
   the Django ORM is unaffected.
2. **Compression** — `segmentby = host_id, category, metric`, `orderby = time DESC`;
   policy compresses chunks older than **7 days** (`VIGIL_TS_COMPRESS_AFTER`).
   Typical metric compression is 10–20×; this is the biggest single win.
3. **Retention** — `drop_chunks` policy drops raw chunks older than
   **30 days** (`VIGIL_METRIC_RETENTION_DAYS`). Dropping a chunk frees its disk
   immediately, unlike DELETE.
4. **Continuous aggregates** — `..._hourly` (kept 1 year) and `..._daily`
   (kept 5 years) rollups per series (`host_id, category, metric, labels`) so
   downsampled trend history survives raw expiry.

The old prune task now no-ops when the table is a hypertable (native retention
owns expiry).

### Tunables (env)

| Setting | Default | Meaning |
|---|---|---|
| `VIGIL_METRIC_RETENTION_DAYS` | `30` | raw retention horizon |
| `VIGIL_TS_COMPRESS_AFTER` | `7 days` | compress chunks older than |
| `VIGIL_TS_CHUNK_INTERVAL` | `1 day` | hypertable chunk size |
| `VIGIL_TS_HOURLY_RETENTION` | `365 days` | 1-hour rollup retention |
| `VIGIL_TS_DAILY_RETENTION` | `1825 days` | 1-day rollup retention |
| `VIGIL_DB_SIZE_WARN_GB` / `VIGIL_DB_SIZE_CRIT_GB` | `20` / `40` | safety-valve thresholds |

## Diagnostics (run on the affected host)

```sql
-- Largest tables
SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_size_pretty(pg_relation_size(c.oid)) AS heap,
       pg_size_pretty(pg_indexes_size(c.oid)) AS indexes
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind='r'
ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 15;

SELECT * FROM timescaledb_information.hypertables;      -- expect none pre-fix
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum
FROM pg_stat_user_tables WHERE relname='metrics_metricpoint';
SELECT min(time), max(time), count(*) FROM metrics_metricpoint;
```

## Recovery on a near-full disk

`create_hypertable(..., migrate_data => true)` copies **every** existing row
into chunks first, needing scratch ≈ the current table size (~26 GB). That will
**fail on a full disk**. Because diagnostics usually show much of the 26 GB is
dead/bloat, use this low-scratch order instead — it needs scratch only for the
*kept* window, not the full table.

> Dropping raw data is destructive. The retention horizon (30 days) was signed
> off before this runbook was written. Confirm before step 3.

1. **Get Postgres up.** If the disk is 100 % full, temporarily add a few GB to
   the volume (online resize) so Postgres can write `postmaster.pid` and WAL.
2. **Free breathing room** on the *plain* table by dropping the oldest raw days
   in batches, then a normal `VACUUM` (not FULL) so the space is reusable:
   ```sql
   DELETE FROM metrics_metricpoint WHERE time < now() - INTERVAL '30 days';
   VACUUM (VERBOSE) metrics_metricpoint;
   ```
3. **Build the rollups from raw before it expires**, so downsampled history is
   preserved. Run migration `metrics/0002` (below) which creates the continuous
   aggregates, then force a full refresh while raw still exists:
   ```sql
   CALL refresh_continuous_aggregate('metrics_metricpoint_hourly', NULL, now());
   CALL refresh_continuous_aggregate('metrics_metricpoint_daily',  NULL, now());
   ```
4. **Apply the schema.** `python manage.py migrate metrics`. The migration is
   idempotent: if you pre-converted the table it skips conversion. On the now
   smaller table, `migrate_data` needs far less scratch.
5. **Compress oldest chunks first** to reclaim space incrementally rather than
   waiting for the background policy:
   ```sql
   SELECT compress_chunk(c, if_not_compressed => true)
   FROM show_chunks('metrics_metricpoint', older_than => INTERVAL '7 days') c;
   ```
6. **Shrink the volume back** once retention + compression have freed space, if
   you expanded it in step 1.

### Even lower scratch (alternative to step 2)

If step 2 can't free enough, create a fresh empty hypertable, copy only the
kept window, then drop the fat table (instant OS reclaim):

```sql
CREATE TABLE metrics_metricpoint_new (LIKE metrics_metricpoint INCLUDING DEFAULTS);
SELECT create_hypertable('metrics_metricpoint_new','time', chunk_time_interval => INTERVAL '1 day');
INSERT INTO metrics_metricpoint_new SELECT * FROM metrics_metricpoint
  WHERE time >= now() - INTERVAL '30 days';
DROP TABLE metrics_metricpoint;                       -- frees ~26 GB immediately
ALTER TABLE metrics_metricpoint_new RENAME TO metrics_metricpoint;
-- then re-add indexes/policies (re-run migrate; it detects the hypertable).
```

## Safety valve

`metrics.check_db_disk_usage` (hourly, Celery beat) logs the database size and
the metrics hypertable's share, WARNING past `VIGIL_DB_SIZE_WARN_GB` and ERROR
past `VIGIL_DB_SIZE_CRIT_GB`, so this class of problem surfaces before it fills
the disk again.
