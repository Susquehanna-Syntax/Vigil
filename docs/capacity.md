# How many hosts one Vigil handles

Measured with the load harness (`manage.py loadtest_seed` and `scripts/loadtest.py`, sprint M4): simulated agents
check in on their own interval with jitter, each sending a payload shaped like the real agent's (19 metric points).
The server ran as production runs it: gunicorn with 3 uvicorn workers, PostgreSQL with TimescaleDB, Redis.

## Results, 16 Nov 2026

| Hosts | Check-in interval | Run | Check-ins | Errors | p50 | p95 | p99 | Metric rows written per second |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 60 s | 5 min | 25,021 | 0 | 16.6 ms | 24.6 ms | 35.8 ms | 1,584 |
| 10,000 | 60 s | 5 min | 50,033 | 0 | 18.2 ms | 33.0 ms | 62.3 ms | 3,165 |

The sprint's target for check-ins was a p95 under 500 ms at 5,000 hosts; both runs meet it. The raw reports are in `docs/capacity/`.

**Machine:** Intel Core Ultra 7 265KF (20 cores), 62 GB RAM, PostgreSQL 17.12 on a SATA SSD (Kingston A400),
Debian 13. The load generator ran on the same machine, so the server had less than all of it.

## A 90-day chart

On TimescaleDB 2.32 (the community build the published image uses), with its rollups, compression and retention
policies as migration 0002 sets them: 500 hosts with 90 days of history (the timed host at one sample a minute, the
others hourly, 19 series each), the hourly and daily rollups filled, raw rows older than 30 days dropped and chunks
older than 7 days compressed, as the policies leave them. That is 7.7 million raw rows, 20.5 million hourly and
845,500 daily rollup rows.

The Monitor page loads seven series for a host (CPU, memory, disk, network in and out, swap, load). For a 90-day range
each is answered from the daily rollup (91 points per series):

| | All seven, one after another |
|---|---|
| First load (cold caches) | 292 ms |
| After that (median of 5) | 115 ms |

The browser requests them in parallel, so the page waits for the slowest one (under 30 ms warm), not their sum. The
target was under 1 second.

## What these numbers do not cover yet

- **TimescaleDB edition for check-ins.** The check-in runs above used Debian's Apache-licensed TimescaleDB (no
  compression or rollups). Ingest does not depend on them, but the runs have not yet been repeated on the community
  build.
- **Background work.** No Celery worker ran, so the work check-ins hand to Celery queued rather than ran on the same
  CPUs. Celery queue depth is not in these reports.
- **Inventory payloads.** The simulated check-ins carry metrics only. Real agents also send hardware inventory hourly,
  software lists when they change and container snapshots; those are not in this load.

To repeat a run:

```
python manage.py loadtest_seed --hosts 5000 --out /tmp/load-tokens.txt
python scripts/loadtest.py --server http://localhost:8000 --tokens /tmp/load-tokens.txt \
    --hosts 5000 --interval 60 --duration 300 --report report.json
```
