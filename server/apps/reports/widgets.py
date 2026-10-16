"""Computing a report's charts from run history.

Every widget is drawn from the same host-run model: a **host-run** is one
``(run, host)`` pair — one host's go at one run — and its outcome is
``apps.tasks.summary._host_state`` over that pair's tasks, so the page never
disagrees with the run detail view about what "failed" means (a failure the
playbook handles with ``on_failure: continue`` is not the host's).

One task query feeds every widget: the rows come back once as dicts and all of
the grouping below happens in Python.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from django.db.models import Q
from django.utils.text import slugify

from apps.tasks.summary import _FAILED, _host_state
from vigil import scoping

#: Go-to markers (QA-36) are server-side bookkeeping, never a host's work.
#: A completed one would otherwise make a host-run whose real steps were all
#: skipped read `ok`, which is why run_summary excludes them too (QA-37).
_EXCLUDED_ACTIONS = ("_goto",)

#: Guard against a report over a whole year of a large fleet turning into an
#: unbounded query. Newest rows win; the page shows a "truncated" notice.
_MAX_TASK_ROWS = 200_000

WIDGETS: dict[str, dict[str, str]] = {
    "success_rate": {
        "title": "Success rate",
        "description": "Share of finished host-runs that succeeded, per day and per source.",
        "chart": "line",
    },
    "runs_per_day": {
        "title": "Runs per day",
        "description": "Host-runs per day by outcome: ok, failed and still pending.",
        "chart": "stacked-bar",
    },
    "failures_by_step": {
        "title": "Failures by step",
        "description": "The steps that fail most often, by failing task across the range.",
        "chart": "bar",
    },
    "branch_paths": {
        "title": "Branch paths",
        "description": "How many host-runs reached each branch of the playbook's flow.",
        "chart": "bar",
    },
    "duration": {
        "title": "Duration",
        "description": "Median and 95th-percentile wall-clock seconds of a host-run, per day.",
        "chart": "line",
    },
    "host_outcomes": {
        "title": "Host outcomes",
        "description": "Each host's newest host-run per source, and how it ended.",
        "chart": "table",
    },
    "automation_fires": {
        "title": "Automation fires",
        "description": "How often each selected automation ran, per day.",
        "chart": "stacked-bar",
    },
}

#: Outcomes that count as a success in the rate.
_OK_STATES = ("ok", "not_applicable", "skipped")

#: Fallback headers for an empty `rows` widget, so the CSV is still readable.
_ROWS_KEYS: dict[str, tuple[str, ...]] = {
    "failures_by_step": ("failed", "source", "step"),
    "branch_paths": ("branch", "hosts", "source"),
    "host_outcomes": ("at", "host", "outcome", "source"),
}


def compute(report, user, now=None) -> dict:
    """Every widget's data for `report`, as the requesting user can see it.

    Sources the user cannot open are dropped, so a shared report never shows
    numbers drawn from a playbook or automation the viewer could not read.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = _days(now, report.range_days)
    start = _day_start(days[0])

    playbooks = {p.pk: p.name for p in _visible(report.playbooks.all(), user)}
    automations = {a.pk: a.name for a in _visible(report.automations.all(), user)}

    runs = _runs_in_scope(start, playbooks, automations)
    sources, run_source = _sources(runs, automations, playbooks)
    # The one task query every widget reads. Its row cap is asked of the
    # queryset itself: slicing at the cap would report every smaller report
    # as truncated too.
    rows = _task_rows(runs, _MAX_TASK_ROWS + 1)
    truncated = len(rows) > _MAX_TASK_ROWS
    tasks = _task_dicts(rows[:_MAX_TASK_ROWS])

    host_runs = _host_runs(runs, run_source, tasks)
    context = {
        "runs": runs,
        "days": days,
        "day_index": {d: i for i, d in enumerate(days)},
        # Every selected source, even one with no runs in the window: a report
        # that names a playbook shows that playbook's (empty) series.
        "sources": sources,
        # name → id, for the automations the viewer can still see.
        "automations": automations,
        "run_source": run_source,
        "host_runs": host_runs,
        "tasks": tasks,
    }
    return {
        "range": {"from": days[0], "to": days[-1], "days": days},
        "truncated": truncated,
        "widgets": [
            {"kind": kind,
             "title": spec.get("title") or WIDGETS[kind]["title"],
             "chart": WIDGETS[kind]["chart"],
             "data": _BUILDERS[kind](context)}
            for kind, spec in _widget_specs(report)
        ],
    }


def to_csv(widget: dict) -> str:
    """A widget's data as CSV — one row per day, or one row per record."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    data = widget.get("data") or {}
    if "days" in data:
        _days_csv(writer, data)
        return buf.getvalue()
    rows = data.get("rows") or []
    keys = sorted(rows[0]) if rows else _ROWS_KEYS[widget["kind"]]
    writer.writerow(keys)
    for row in rows:
        writer.writerow([_cell(row.get(k)) for k in keys])
    return buf.getvalue()


def csv_filename(report, kind: str) -> str:
    return f"{slugify(report.name, allow_unicode=True) or 'report'}-{kind}.csv"


# -- the widgets ---------------------------------------------------------------

def _success_rate(ctx):
    days = ctx["days"]
    counts: dict = {}
    for hr in ctx["host_runs"]:
        if hr["outcome"] == "pending":
            continue
        bucket = counts.setdefault((hr["source"], hr["day"]), [0, 0])
        bucket[1] += 1
        if hr["outcome"] in _OK_STATES:
            bucket[0] += 1
    series = []
    for name in ctx["sources"]:
        values = []
        for day in days:
            ok, total = counts.get((name, day), (0, 0))
            values.append(round(ok * 100.0 / total, 1) if total else None)
        series.append({"name": name, "values": values})
    return {"days": days, "series": series}


def _runs_per_day(ctx):
    days = ctx["days"]
    ok = [0] * len(days)
    failed = [0] * len(days)
    pending = [0] * len(days)
    for hr in ctx["host_runs"]:
        idx = ctx["day_index"][hr["day"]]
        if hr["outcome"] == "failed":
            failed[idx] += 1
        elif hr["outcome"] == "pending":
            pending[idx] += 1
        else:
            ok[idx] += 1
    return {"days": days, "ok": ok, "failed": failed, "pending": pending}


def _failures_by_step(ctx):
    counts: dict = {}
    for task in ctx["tasks"]:
        if task["state"] not in _FAILED or task["on_failure"] == "continue":
            continue
        key = (ctx["run_source"][task["run"]], task["step_ref"] or "(task)")
        counts[key] = counts.get(key, 0) + 1
    rows = [{"source": source, "step": step, "failed": n}
            for (source, step), n in counts.items()]
    rows.sort(key=lambda r: (-r["failed"], r["step"], r["source"]))
    return {"rows": rows[:15]}


def _branch_paths(ctx):
    seen: dict = {}
    for task in ctx["tasks"]:
        if not task["branch"] or task["state"] == "skipped":
            continue
        # Distinct host-runs: a host that ran the playbook twice took the
        # branch twice.
        seen[(ctx["run_source"][task["run"]], task["branch"], task["run"], task["host_id"])] = True
    counts: dict = {}
    for source, branch, _run, _host in seen:
        key = (source, branch)
        counts[key] = counts.get(key, 0) + 1
    rows = [{"source": source, "branch": branch, "hosts": n}
            for (source, branch), n in counts.items()]
    rows.sort(key=lambda r: (r["source"], r["branch"]))
    return {"rows": rows}


def _duration(ctx):
    days = ctx["days"]
    seconds: dict = defaultdict(list)
    for hr in ctx["host_runs"]:
        if hr["seconds"] is not None:
            seconds[hr["day"]].append(hr["seconds"])
    median, p95 = [], []
    for day in days:
        vals = sorted(seconds.get(day, []))
        median.append(_percentile(vals, 0.5))
        p95.append(_percentile(vals, 0.95))
    return {"days": days, "median": median, "p95": p95}


def _host_outcomes(ctx):
    newest: dict = {}
    for hr in ctx["host_runs"]:
        key = (hr["host_id"], hr["source"])
        if key not in newest or hr["at"] > newest[key]["at"]:
            newest[key] = hr
    rows = [{"host": hr["host"], "source": hr["source"],
             "outcome": hr["outcome"], "at": _iso(hr["at"])}
            for hr in newest.values()]
    rows.sort(key=lambda r: (r["host"], r["source"]))
    return {"rows": rows}


def _automation_fires(ctx):
    """Runs created per day per automation, one series per visible one.

    Keyed by name, but counted by id: an automation the viewer cannot see has
    no id here, so its runs drop out of the widget entirely.
    """
    days = ctx["days"]
    by_id = {str(pk): name for pk, name in ctx["automations"].items()}
    counts: dict = {name: [0] * len(days) for name in by_id.values()}
    for run in ctx["runs"]:
        name = by_id.get(str(run["automation"])) if run["automation"] else None
        if name is not None:
            counts[name][ctx["day_index"][run["day"]]] += 1
    return {"days": days,
            "series": [{"name": name, "values": counts[name]}
                       for name in sorted(counts)]}


_BUILDERS = {
    "success_rate": _success_rate,
    "runs_per_day": _runs_per_day,
    "failures_by_step": _failures_by_step,
    "branch_paths": _branch_paths,
    "duration": _duration,
    "host_outcomes": _host_outcomes,
    "automation_fires": _automation_fires,
}


# -- the shared passes ---------------------------------------------------------

def _visible(qs, user):
    return list(scoping.filter_by_site(qs, user, cascade_global=True))


def _sources(runs, automation_names, playbook_names):
    """The report's source names and each run's attribution.

    Every selected source is listed, automations first — one with no runs in
    the window still gets a series, so the chart does not silently drop a
    playbook someone put in the report. Names are the display key, so two
    sources sharing one are listed once and their runs draw together.
    """
    names = list(automation_names.values()) + list(playbook_names.values())
    run_source = {}
    for run in runs:
        # A run from an automation that runs a playbook belongs to exactly one
        # source, and it is the automation's.
        run_source[run["pk"]] = (automation_names.get(run["automation"])
                                 or playbook_names.get(run["playbook"], ""))
    return list(dict.fromkeys(name for name in names if name)), run_source


def _runs_in_scope(start, playbook_names, automation_names):
    from apps.tasks.models import TaskRun

    if not playbook_names and not automation_names:
        return []
    scope = Q()
    if playbook_names:
        scope |= Q(playbook_id__in=playbook_names)
    if automation_names:
        scope |= Q(automation_id__in=automation_names)
    runs = (TaskRun.objects.filter(created_at__gte=start).filter(scope)
            .order_by("-created_at")
            .values("id", "created_at", "playbook_id", "automation_id"))
    return [{"pk": run["id"], "created_at": run["created_at"],
             "playbook": run["playbook_id"], "automation": run["automation_id"],
             "day": run["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")}
            for run in runs]


def _task_rows(runs, limit):
    from apps.tasks.models import Task

    run_ids = [run["pk"] for run in runs]
    if not run_ids:
        return []
    return list(
        Task.objects.filter(run_id__in=run_ids)
        .exclude(action__in=_EXCLUDED_ACTIONS)
        .order_by("-created_at")
        .values("run_id", "host_id", "host__hostname", "state", "on_failure",
                "step_ref", "branch", "created_at", "dispatched_at",
                "completed_at")[:limit]
    )


def _task_dicts(rows):
    return [{"run": row["run_id"], "host_id": row["host_id"], "host": row["host__hostname"],
             "state": row["state"], "on_failure": row["on_failure"],
             "step_ref": row["step_ref"], "branch": row["branch"],
             "created_at": row["created_at"], "dispatched_at": row["dispatched_at"],
             "completed_at": row["completed_at"]}
            for row in rows]


def _host_runs(runs, run_source, tasks):
    """One record per (run, host): outcome, day, source and duration."""
    grouped: dict = {}
    for task in tasks:
        # By host id: two hosts may share a hostname.
        grouped.setdefault((task["run"], task["host_id"]), []).append(task)
    day_of = {run["pk"]: run["day"] for run in runs}
    at_of = {run["pk"]: run["created_at"] for run in runs}
    out = []
    for (run_pk, host_id), rows in grouped.items():
        finished = [t["completed_at"] for t in rows if t["completed_at"]]
        starts = [t["dispatched_at"] or t["created_at"] for t in rows]
        seconds = None
        if finished and starts:
            delta = (max(finished) - min(starts)).total_seconds()
            if delta >= 0:
                seconds = delta
        out.append({
            "run": run_pk,
            "host_id": host_id,
            "host": rows[0]["host"],
            "source": run_source[run_pk],
            "day": day_of[run_pk],
            "at": at_of[run_pk],
            "outcome": _host_state([_HostTask(t) for t in rows]),
            "seconds": seconds,
        })
    return out


def _widget_specs(report):
    """The report's widgets, sanitised: unknown kinds and duplicates dropped."""
    out, seen = [], set()
    for spec in report.widgets or []:
        if not isinstance(spec, dict):
            continue
        kind = spec.get("kind")
        if kind not in WIDGETS or kind in seen:
            continue
        seen.add(kind)
        out.append((kind, spec))
    return out


class _HostTask:
    """The two attributes ``_host_state`` reads, from a task dict."""

    __slots__ = ("on_failure", "state")

    def __init__(self, task):
        self.state = task["state"]
        self.on_failure = task["on_failure"]


def _days(now, range_days) -> list[str]:
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=range_days - 1)
    return [(first + timedelta(days=i)).isoformat() for i in range(range_days)]


def _day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def _percentile(values, fraction):
    """Nearest-rank percentile in whole seconds; None when empty.

    Whole seconds because the chart reads a duration off a line, and a
    host-run's length is only measured to the second anyway.
    """
    if not values:
        return None
    rank = max(1, math.ceil(fraction * len(values)))
    return round(values[min(rank, len(values)) - 1])


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _cell(value):
    return "" if value is None else value


def _days_csv(writer, data):
    days = data["days"]
    if "series" in data:
        series = data["series"]
        writer.writerow(["day"] + [s["name"] for s in series])
        for i, day in enumerate(days):
            writer.writerow([day] + [_cell(s["values"][i]) for s in series])
        return
    if "median" in data:
        writer.writerow(["day", "median", "p95"])
        for i, day in enumerate(days):
            writer.writerow([day, _cell(data["median"][i]), _cell(data["p95"][i])])
        return
    writer.writerow(["day", "ok", "failed", "pending"])
    for i, day in enumerate(days):
        writer.writerow([day, data["ok"][i], data["failed"][i], data["pending"][i]])
