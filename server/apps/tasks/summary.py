"""How a run went, per host and in total (M6 08c).

Each host ends in one state — pending, failed, not applicable, skipped or ok —
and, when the task or playbook names outcomes on its steps ("Patched",
"Recovered"), in the outcome of the last labelled step that succeeded there.
Run detail draws these as donuts; the task library shows its latest run's.
"""
from __future__ import annotations

from collections import Counter

_ACTIVE = {"blocked", "pending", "dispatched", "executing"}
_FAILED = {"failed", "rejected", "expired"}


def _host_state(tasks) -> str:
    states = {t.state for t in tasks}
    if states & _ACTIVE:
        return "pending"
    # A failure a playbook step handles (on_failure: continue) is not the host's.
    if any(t.state in _FAILED and t.on_failure != "continue" for t in tasks):
        return "failed"
    if "completed" in states or any(t.state in _FAILED for t in tasks):
        return "ok"
    if "not_applicable" in states:
        return "not_applicable"
    return "skipped"


def _task_outcome_labels(run) -> dict[str, str]:
    """Task runs: step id → outcome, from the definition's own steps."""
    spec = (run.definition.parsed_spec or {}) if run.definition_id else {}
    return {a["id"]: a["outcome"] for a in spec.get("actions") or [] if a.get("outcome")}


def _host_outcome(tasks, run, task_labels: dict[str, str]) -> str | None:
    snap = run.flow_snapshot or {}
    step_labels = {s["step_id"]: s.get("outcome") for s in snap.get("steps") or [] if s.get("outcome")}
    outcome = None
    for task in sorted(tasks, key=lambda t: t.step_order):
        if step_labels:
            if task.state == "completed" and step_labels.get(task.step_ref):
                outcome = step_labels[task.step_ref]
            continue
        for step in (task.result_data or {}).get("steps") or []:
            if isinstance(step, dict) and step.get("status") == "ok" and task_labels.get(step.get("id")):
                outcome = task_labels[step["id"]]
    return outcome


def run_summary(run) -> dict:
    """``{"hosts", "states": {state: n}, "outcomes": {name: n} | None,
    "per_host": {host_id: {"state", "outcome"}}}``."""
    by_host: dict = {}
    for task in run.tasks.all():
        by_host.setdefault(task.host_id, []).append(task)
    task_labels = _task_outcome_labels(run)
    labelled = bool(task_labels) or any(
        s.get("outcome") for s in (run.flow_snapshot or {}).get("steps") or [])
    per_host = {}
    for host_id, tasks in by_host.items():
        per_host[str(host_id)] = {
            "state": _host_state(tasks),
            "outcome": _host_outcome(tasks, run, task_labels) if labelled else None,
        }
    states = Counter(h["state"] for h in per_host.values())
    outcomes = None
    if labelled:
        outcomes = Counter((h["outcome"] or "No outcome yet") for h in per_host.values())
    return {"hosts": len(per_host), "states": dict(states),
            "outcomes": dict(outcomes) if outcomes is not None else None,
            "per_host": per_host}
