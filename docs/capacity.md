# How many hosts one Vigil handles

Measured with the load harness (`manage.py loadtest_seed` and `scripts/loadtest.py`, sprint M4): simulated agents
check in on their own interval with jitter, each sending a payload shaped like the real agent's (19 metric points).
The server ran as production runs it: gunicorn with 3 uvicorn workers, PostgreSQL with TimescaleDB, Redis.

## Results, 16 Nov 2026

| Hosts | Check-in interval | Run | Check-ins | Errors | p50 | p95 | p99 | Metric rows written per second |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 60 s | 5 min | 25,021 | 0 | 16.6 ms | 24.6 ms | 35.8 ms | 1,584 |
| 10,000 | 60 s | 5 min | 50,033 | 0 | 18.2 ms | 33.0 ms | 62.3 ms | 3,165 |

The sprint's target for check-ins was a p95 under 500 ms at 5,000 hosts. The raw reports are in `docs/capacity/`.

**Machine:** Intel Core Ultra 7 265KF (20 cores), 62 GB RAM, PostgreSQL 17.12 on a SATA SSD (Kingston A400),
Debian 13. The load generator ran on the same machine, so the server had less than all of it.

## What these numbers do not cover yet

- **TimescaleDB edition.** This run used Debian's Apache-licensed TimescaleDB, which has hypertables but no
  compression, continuous aggregates or retention policies. Ingest (what check-ins measure) does not depend on them;
  the long-range charts do. The 90-day chart target (under 1 second) is measured on the community build, which the
  published Docker image uses.
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
