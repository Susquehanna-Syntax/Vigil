#!/usr/bin/env python3
"""Thousands of pretend agents against a real Vigil server (sprint M4 phase 01).

    python manage.py loadtest_seed --hosts 2000 --out /tmp/load-tokens.txt
    python scripts/loadtest.py --server http://localhost:8000 --tokens /tmp/load-tokens.txt \\
        [--hosts N] [--interval 30] [--duration 300] [--metrics-token T] [--report out.json]

Each pretend agent checks in on its own interval with jitter, sending a payload
shaped like the real agent's (19 metric points). At the end it prints check-in
latency p50/p95/p99, errors by status, check-ins and metric rows per second,
and the largest Celery queue /metrics showed (with --metrics-token). Exit code
1 if any check-in failed. Standard library plus aiohttp only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from datetime import datetime, timezone


def payload(hostname: str, rng: random.Random) -> dict:
    """A check-in body shaped like the real agent's (agent/vigil_agent/client.py
    and collector.py): 19 metric points."""
    now = datetime.now(timezone.utc).isoformat()
    points = []

    def add(category, metric, value, labels=None):
        points.append({"category": category, "metric": metric, "value": round(value, 2),
                       "labels": labels or {}, "time": now})

    for core in ("0", "1", "2", "3", "total"):
        add("cpu", "usage_percent", rng.uniform(1, 95), {"core": core})
    for window in ("load_1m", "load_5m", "load_15m"):
        add("cpu", window, rng.uniform(0, 4))
    total_mem = 8 * 1024 ** 3
    used = rng.uniform(0.2, 0.9) * total_mem
    add("memory", "usage_percent", 100 * used / total_mem)
    add("memory", "used_bytes", used)
    add("memory", "total_bytes", total_mem)
    for mount in ("/", "/var"):
        total_disk = 100 * 1024 ** 3
        used_disk = rng.uniform(0.1, 0.95) * total_disk
        add("disk", "usage_percent", 100 * used_disk / total_disk, {"mount": mount})
        add("disk", "used_bytes", used_disk, {"mount": mount})
        add("disk", "total_bytes", total_disk, {"mount": mount})
    add("network", "bytes_sent", rng.uniform(1e6, 1e10), {"interface": "eth0"})
    add("network", "bytes_recv", rng.uniform(1e6, 1e10), {"interface": "eth0"})
    return {"hostname": hostname, "os": "Debian GNU/Linux 12", "kernel": "6.1.0-loadtest",
            "vigil_version": "loadtest", "mode": "managed", "features": [], "allowlist": [],
            "metrics": points}


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile, p in 0..100; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarize(latencies_ms: list[float], statuses: dict[int, int], points_sent: int,
              seconds: float, max_queue: int | None) -> dict:
    ok = sum(n for s, n in statuses.items() if 200 <= s < 300)
    seconds = seconds or 1.0
    return {
        "checkins": ok,
        "errors": {s: n for s, n in sorted(statuses.items()) if not 200 <= s < 300},
        "p50_ms": round(percentile(latencies_ms, 50), 1),
        "p95_ms": round(percentile(latencies_ms, 95), 1),
        "p99_ms": round(percentile(latencies_ms, 99), 1),
        "checkins_per_s": round(ok / seconds, 1),
        "rows_per_s": round(points_sent / seconds, 1),
        "max_celery_queue": max_queue,
    }


async def run(args) -> int:
    import aiohttp

    with open(args.tokens) as fh:
        tokens = [line.strip() for line in fh if line.strip()]
    if args.hosts:
        tokens = tokens[:args.hosts]
    if not tokens:
        print("no tokens", file=sys.stderr)
        return 2

    server = args.server.rstrip("/")
    rng = random.Random(getattr(args, "seed", 1234))
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    sent = [0]
    max_queue: list[int | None] = [None]
    start = time.monotonic()
    deadline = start + args.duration

    async def agent(session, token, host_rng):
        hostname = "-".join(token.split("-")[:2])
        await asyncio.sleep(host_rng.uniform(0, args.interval))
        while time.monotonic() < deadline:
            body = payload(hostname, host_rng)
            began = time.perf_counter()
            try:
                async with session.post(f"{server}/api/v1/checkin", json=body,
                                        headers={"Authorization": f"Bearer {token}"}) as resp:
                    await resp.read()
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError):
                status = 0
            statuses[status] = statuses.get(status, 0) + 1
            if 200 <= status < 300:
                latencies.append((time.perf_counter() - began) * 1000)
                sent[0] += len(body["metrics"])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, args.interval * host_rng.uniform(0.9, 1.1)))

    async def scrape(session):
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{server}/metrics",
                                       headers={"Authorization": f"Bearer {args.metrics_token}"}) as resp:
                    for line in (await resp.text()).splitlines():
                        if line.startswith("vigil_celery_queue_depth "):
                            depth = int(float(line.split()[1]))
                            max_queue[0] = depth if max_queue[0] is None else max(max_queue[0], depth)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, IndexError):
                pass
            await asyncio.sleep(min(10, max(0.0, deadline - time.monotonic())))

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=200),
                                     timeout=aiohttp.ClientTimeout(total=30)) as session:
        jobs = [agent(session, t, random.Random(rng.randrange(2 ** 32))) for t in tokens]
        if args.metrics_token:
            jobs.append(scrape(session))
        await asyncio.gather(*jobs)

    report = summarize(latencies, statuses, sent[0], time.monotonic() - start, max_queue[0])
    report["hosts"] = len(tokens)
    report["interval_s"] = args.interval
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(text + "\n")
    return 1 if report["errors"] else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", required=True)
    parser.add_argument("--tokens", required=True)
    parser.add_argument("--hosts", type=int, default=0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--metrics-token")
    parser.add_argument("--report")
    parser.add_argument("--seed", type=int, default=1234)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
