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

#: Per kind: the default `chart` plus the `charts` that suit it, the axis
#: label defaults, the `family` the browser draws it for ("days" is one series
#: per day, "rows" one row per record), the plain-words `how` the metric is
#: worked out, and the default card `size` on a 12-column grid.
WIDGETS: dict[str, dict] = {
    "success_rate": {
        "title": "Success rate",
        "description": "Share of finished host-runs that succeeded, per day and per source.",
        "chart": "line",
        "family": "days",
        "charts": ["line", "area", "bar", "table"],
        "x_label": "Day",
        "y_label": "Succeeded (%)",
        "how": ("For each day, the share of host-runs that ended ok, not applicable "
                "or skipped, out of every host-run that finished. A host-run is one "
                "host's go at one run; a failure the playbook handles with carry on "
                "does not count against it. One line per playbook or automation."),
        "size": {"w": 4, "h": 4},
    },
    "runs_per_day": {
        "title": "Runs per day",
        "description": "Host-runs per day by outcome: ok, failed and still pending.",
        "chart": "stacked-bar",
        "family": "days",
        "charts": ["stacked-bar", "bar", "line", "area", "table"],
        "x_label": "Day",
        "y_label": "Host-runs",
        "how": ("For each day, how many host-runs started, split by how they ended: "
                "ok, failed, or still running."),
        "size": {"w": 4, "h": 4},
    },
    "failures_by_step": {
        "title": "Failures by step",
        "description": "The steps that fail most often, by failing task across the range.",
        "chart": "bar",
        "family": "rows",
        "charts": ["bar", "column", "pie", "doughnut", "table"],
        "x_label": "Failures",
        "y_label": "Step",
        "how": ("Failed tasks across the whole range, counted per playbook step. "
                "Failures the step handles with carry on are left out. The 15 steps "
                "that fail most are shown."),
        "size": {"w": 4, "h": 4},
    },
    "branch_paths": {
        "title": "Branch paths",
        "description": "How many host-runs reached each branch of the playbook's flow.",
        "chart": "bar",
        "family": "rows",
        "charts": ["bar", "column", "pie", "doughnut", "table"],
        "x_label": "Host-runs",
        "y_label": "Branch",
        "how": ("How many host-runs took each exit of each decision in the playbook's "
                "flow, across the whole range. A host that ran the playbook twice "
                "counts twice."),
        "size": {"w": 4, "h": 4},
    },
    "duration": {
        "title": "Duration",
        "description": "Median and 95th-percentile wall-clock seconds of a host-run, per day.",
        "chart": "line",
        "family": "days",
        "charts": ["line", "area", "bar", "table"],
        "x_label": "Day",
        "y_label": "Time per host-run",
        "how": ("For each day, the median and the 95th-percentile time from a "
                "host-run's first task starting to its last task finishing."),
        "size": {"w": 4, "h": 4},
    },
    "host_outcomes": {
        "title": "Host outcomes",
        "description": "Each host's newest host-run per source, and how it ended.",
        "chart": "table",
        "family": "rows",
        "charts": ["table", "pie", "doughnut", "column"],
        "x_label": "Outcome",
        "y_label": "Hosts",
        "how": ("Each host's most recent host-run per playbook or automation, and how "
                "it ended. As a chart, the hosts are counted per outcome."),
        "size": {"w": 4, "h": 4},
    },
    "automation_fires": {
        "title": "Automation fires",
        "description": "How often each selected automation ran, per day.",
        "chart": "stacked-bar",
        "family": "days",
        "charts": ["stacked-bar", "bar", "line", "area", "table"],
        "x_label": "Day",
        "y_label": "Runs",
        "how": "For each day, how many runs each selected automation started.",
        "size": {"w": 4, "h": 4},
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
    """Every widget's data for `report`, as the requesting user can see it."""
    return compute_for(report.playbooks.all(), report.automations.all(),
                       report.range_days, report.widgets, user, now)


def compute_for(playbooks_qs, automations_qs, range_days, widget_specs, user,
                now=None) -> dict:
    """The same, from a source selection rather than a saved report — so the
    editor can preview a chart before anything is written down.

    Sources the user cannot open are dropped, so a shared report never shows
    numbers drawn from a playbook or automation the viewer could not read.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = _days(now, range_days)
    start = _day_start(days[0])

    playbooks = {p.pk: p.name for p in _visible(playbooks_qs, user)}
    automations = {a.pk: a.name for a in _visible(automations_qs, user)}

    runs = _runs_in_scope(start, playbooks, automations)
    sources, run_source = _sources(runs, automations, playbooks)
    # The one task query every widget reads. Its row cap is asked of the
    # queryset itself: slicing at the cap would report every smaller report
    # as truncated too.
    rows = _task_rows(runs, user, _MAX_TASK_ROWS + 1)
    truncated = len(rows) > _MAX_TASK_ROWS
    tasks = _task_dicts(rows[:_MAX_TASK_ROWS])
    # A source the viewer may open can still run on hosts they may not (a
    # global playbook runs in every site). Only runs with a task on a host
    # they can see count, so a run on a hidden host adds nothing anywhere,
    # automation fires included.
    seen_runs = {task["run"] for task in tasks}
    runs = [run for run in runs if run["pk"] in seen_runs]

    host_runs = _host_runs(runs, run_source, tasks)
    context = {
        "runs": runs,
        "days": days,
        "day_index": {d: i for i, d in enumerate(days)},
        # Every selected source, even one with no runs in the window: a report
        # that names a playbook shows that playbook's (empty) series.
        "sources": sources,
        # id → name, for the sources a widget may narrow itself to.
        "playbooks": playbooks,
        "automations": automations,
        "run_source": run_source,
        "host_runs": host_runs,
        "tasks": tasks,
    }
    return {
        "range": {"from": days[0], "to": days[-1], "days": days},
        "truncated": truncated,
        "widgets": [_widget(kind, spec, context) for kind, spec in _widget_specs(widget_specs)],
    }


#: The charts drawn with an x and a y axis; pie, doughnut and table have none.
AXIS_CHARTS = ("line", "area", "bar", "stacked-bar", "column")


def default_labels(kind: str, chart: str) -> tuple[str, str]:
    """The (horizontal, vertical) axis titles a chart gets when none are set.

    The catalogue's labels are written for the kind's default chart. A rows
    metric drawn by default as bars across (value along x) turns its axes round
    when drawn as columns, so its labels turn round with them.
    """
    spec = WIDGETS[kind]
    if chart == "column" and spec["chart"] == "bar":
        return spec["y_label"], spec["x_label"]
    return spec["x_label"], spec["y_label"]


def _widget(kind: str, spec: dict, context: dict) -> dict:
    """One computed widget: its drawing options, the sources it covers, and its
    data — narrowed to those sources when the spec names any."""
    defaults = WIDGETS[kind]
    chart = spec.get("chart") or defaults["chart"]
    x_default, y_default = default_labels(kind, chart)
    covered = _covered_names(context, spec.get("sources"))
    return {
        "kind": kind,
        "title": spec.get("title") or defaults["title"],
        "chart": chart,
        "x_label": spec["x_label"] if "x_label" in spec else x_default,
        "y_label": spec["y_label"] if "y_label" in spec else y_default,
        # None leaves the call to the browser.
        "legend": spec.get("legend"),
        "layout": ({"x": spec["x"], "y": spec["y"], "w": spec["w"], "h": spec["h"]}
                   if "x" in spec else None),
        "sources": covered,
        "data": _BUILDERS[kind](_narrow(context, covered) if covered is not None
                                else context),
    }


def _covered_names(context: dict, source_ids) -> list[str] | None:
    """The names of the sources one widget covers, or None for all of them.

    Ids the report does not name (or the viewer cannot open) are ignored; if
    none of them are left the widget covers nothing, never everything.
    """
    if not source_ids:
        return None
    wanted = {str(pk) for pk in source_ids}
    names = {name for name, ids in _sources_by_name(context).items() if ids & wanted}
    return [name for name in context["sources"] if name in names]


def _narrow(context: dict, names: list[str]) -> dict:
    """`context` kept to the sources named. Anything the builders read that
    carries a source goes: each run keeps one source name, so every row built
    from a dropped run drops with it."""
    covered = set(names)
    return {
        **context,
        "sources": [name for name in context["sources"] if name in covered],
        "runs": [run for run in context["runs"]
                 if context["run_source"][run["pk"]] in covered],
        "host_runs": [run for run in context["host_runs"] if run["source"] in covered],
        "tasks": [task for task in context["tasks"]
                  if context["run_source"][task["run"]] in covered],
        "automations": {pk: name for pk, name in context["automations"].items()
                        if name in covered},
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
    writer.writerow([_cell(k) for k in keys])
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


def _sources(runs, automation_names: dict, playbook_names: dict):
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


def _sources_by_name(context: dict) -> dict:
    """The ids that draw each source name — both of a report's sources when two
    share a display name, the automation id included.

    Sources the viewer cannot open are not in the context at all, so a widget
    naming one covers nothing rather than falling back to everything.
    """
    by_name = defaultdict(set)
    for pk, name in {**context["playbooks"], **context["automations"]}.items():
        by_name[name].add(str(pk))
    return by_name


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


def _task_rows(runs, user, limit):
    from apps.tasks.models import Task

    run_ids = [run["pk"] for run in runs]
    if not run_ids:
        return []
    qs = Task.objects.filter(run_id__in=run_ids).exclude(action__in=_EXCLUDED_ACTIONS)
    # Hosts the viewer cannot see stay out of every widget.
    qs = scoping.filter_by_site(qs, user, path="host__")
    return list(
        qs.order_by("-created_at")
        .values("id", "run_id", "host_id", "host__hostname", "state", "on_failure",
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


def _widget_specs(widgets_list):
    """The report's widgets, sanitised: unknown kinds dropped, duplicates kept —
    one metric may be charted twice, once as a line and once as a table."""
    out = []
    for spec in (widgets_list if isinstance(widgets_list, list) else []):
        if not isinstance(spec, dict):
            continue
        kind = spec.get("kind")
        if kind not in WIDGETS:
            continue
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


#: A cell starting with one of these is read as a formula by spreadsheet apps.
#: Hostnames, step ids and source names are user- or agent-supplied, so a
#: quote in front keeps them as text when the CSV is opened.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _days_csv(writer, data):
    days = data["days"]
    if "series" in data:
        series = data["series"]
        writer.writerow(["day"] + [_cell(s["name"]) for s in series])
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
