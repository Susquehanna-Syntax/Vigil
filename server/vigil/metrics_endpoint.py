"""Prometheus ``GET /metrics`` — Vigil's own health, read at scrape time (S3 05b).

Every number comes from the database or the broker on each scrape. In-process
counters would lie: Vigil runs several gunicorn workers and one worker's
counter is a random fraction of the truth.

Without ``VIGIL_METRICS_TOKEN`` the endpoint does not exist, so nothing is
exposed by default.
"""
import hmac
import os
import time

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseNotFound

from vigil import scoping

#: How recent a check-in must be to count as "checked in", per window.
_CHECKIN_WINDOWS = [("1m", 60), ("5m", 300), ("1h", 3600)]


def _metric(lines, name, kind, help_text, samples):
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        label_txt = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        lines.append(f"{name}{{{label_txt}}} {value}" if label_txt else f"{name} {value}")


def _celery_queue_depth():
    """Messages waiting in the default queue, or None when the broker does not answer."""
    try:
        import redis
        url = settings.CELERY_BROKER_URL
        if not url.startswith(("redis://", "rediss://")):
            return None
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        return int(client.llen("celery"))
    except Exception:  # noqa: BLE001 - a scrape must not fail because the broker is down
        return None


def collect() -> dict:
    """Every number /metrics serves, as data. Read now, from the database and
    the broker. Callers run in a system scope (the counts are install-wide)."""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from apps.hosts.models import Host
    from apps.remote.models import RemoteSession
    from apps.tasks.models import Task

    now = timezone.now()
    depth = _celery_queue_depth()
    return {
        "hosts": {status.value: Host.objects.filter(status=status).count()
                  for status in Host.Status},
        "checked_in": {label: Host.objects.filter(
                           last_checkin__gte=now - timedelta(seconds=seconds)).count()
                       for label, seconds in _CHECKIN_WINDOWS},
        "remote_sessions_open": RemoteSession.objects.filter(
            state=RemoteSession.State.OPEN).count(),
        "tasks": {row["state"]: row["n"]
                  for row in Task.objects.values("state").annotate(n=Count("id"))},
        "celery_broker_up": depth is not None,
        "celery_queue_depth": depth,
    }


@scoping.system_view(reason="Prometheus scrape: no person is behind it; the bearer token is the access control")
def metrics(request):
    token = (os.environ.get("VIGIL_METRICS_TOKEN") or "").strip()
    if not token:
        return HttpResponseNotFound()

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    given = request.META.get("HTTP_AUTHORIZATION", "")
    if not hmac.compare_digest(given.encode(), ("Bearer " + token).encode()):
        return HttpResponse(status=401, headers={"WWW-Authenticate": "Bearer"})

    started = time.monotonic()
    data = collect()
    lines: list[str] = []

    _metric(lines, "vigil_hosts", "gauge", "Hosts by enrolment status.",
            [({"status": status}, count) for status, count in data["hosts"].items()])

    _metric(lines, "vigil_hosts_checked_in", "gauge",
            "Hosts whose last check-in is within the window.",
            [({"window": window}, count) for window, count in data["checked_in"].items()])

    _metric(lines, "vigil_remote_sessions_open", "gauge", "Remote sessions currently open.",
            [({}, data["remote_sessions_open"])])

    _metric(lines, "vigil_tasks", "gauge", "Tasks by state.",
            [({"state": state}, n) for state, n in data["tasks"].items()])

    depth = data["celery_queue_depth"]
    _metric(lines, "vigil_celery_broker_up", "gauge", "1 when the Celery broker answered.",
            [({}, 0 if depth is None else 1)])
    if depth is not None:
        _metric(lines, "vigil_celery_queue_depth", "gauge",
                "Messages waiting in the default Celery queue.", [({}, depth)])

    _metric(lines, "vigil_scrape_duration_seconds", "gauge", "Time to build this response.",
            [({}, round(time.monotonic() - started, 4))])

    response = HttpResponse("\n".join(lines) + "\n",
                            content_type="text/plain; version=0.0.4; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response
