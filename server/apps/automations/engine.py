"""Dispatch logic shared by event and scheduled automations.

Both paths converge on :func:`run_automation`, which resolves the target
host(s), builds the agent steps from the automation's task or baseline, and
creates the same signed-task rows as a manual deploy. Nothing here bypasses
the agent's allowlist — an automation is just an automatic *request*.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger("vigil.automations")

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _steps_for(automation) -> tuple[list[dict], str] | None:
    """Return (agent_steps, risk) for the automation's action, or None if the
    action is unresolvable (deleted definition, unknown baseline, ineligible)."""
    from apps.baselines.expansion import BaselineExpandError, expand_actions, _max_risk
    from apps.baselines.models import Baseline, build_agent_steps

    if automation.action_kind == automation.ActionKind.BASELINE:
        baseline = Baseline.objects.filter(
            name__iexact=automation.baseline_name).prefetch_related(
            "steps__definition").first()
        if baseline is None:
            logger.warning("automation %s: no baseline named %r",
                           automation.pk, automation.baseline_name)
            return None
        try:
            return build_agent_steps(baseline)
        except BaselineExpandError as exc:
            logger.warning("automation %s: baseline expand failed: %s",
                           automation.pk, exc)
            return None

    definition = automation.task_definition
    if definition is None:
        return None
    spec = definition.parsed_spec or {}
    try:
        actions, risk = expand_actions(spec.get("actions") or [])
    except BaselineExpandError as exc:
        logger.warning("automation %s: task expand failed: %s", automation.pk, exc)
        return None
    steps = []
    success = spec.get("success_criteria") or None
    for i, a in enumerate(actions):
        step = {"id": f"step{i + 1}", "action": a["type"], "params": a.get("params") or {}}
        if a.get("when"):
            step["when"] = a["when"]
        if success:
            step["success_criteria"] = success
        steps.append(step)
    return steps, _max_risk(spec.get("risk", "standard"), risk)


def _resolve_hosts(automation, event_host):
    from apps.hosts.models import Host

    T = automation.Target
    if automation.target == T.EVENT_HOST:
        return [event_host] if event_host is not None else []
    if automation.target == T.HOST:
        return [automation.target_host] if automation.target_host_id else []

    qs = Host.objects.exclude(status=Host.Status.PENDING).exclude(
        status=Host.Status.REJECTED).exclude(mode=Host.Mode.MONITOR)
    if automation.target == T.TAGS:
        wanted = {str(t).lower() for t in (automation.target_tags or [])}
        if not wanted:
            return []
        return [h for h in qs if wanted & {str(t).lower() for t in (h.tags or [])}]
    return list(qs)  # ALL


def run_automation(automation, *, event_host=None) -> int:
    """Execute *automation*, returning the number of hosts it dispatched to.
    Never raises — an automation failure must not break the event that fired
    it or the beat loop."""
    from django.utils.timezone import now

    from apps.tasks.models import Task

    try:
        built = _steps_for(automation)
        if not built:
            return 0
        steps, risk = built
        if not steps:
            return 0
        hosts = _resolve_hosts(automation, event_host)
        # A monitor-mode host can't execute; skip it silently.
        hosts = [h for h in hosts if getattr(h, "mode", None) != "monitor"]
        created = 0
        label = (automation.baseline_name if automation.action_kind == "baseline"
                 else (automation.task_definition.name if automation.task_definition else automation.name))
        for host in hosts:
            Task.objects.create(
                host=host,
                requested_by=automation.created_by,
                step_label=f"automation: {automation.name} → {label}",
                action="_script",
                params={"steps": steps},
                risk_level=risk,
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
            )
            created += 1
        if created:
            automation.last_run = now()
            automation.run_count = (automation.run_count or 0) + created
            automation.save(update_fields=["last_run", "run_count"])
        return created
    except Exception:  # noqa: BLE001
        logger.exception("automation %s failed to run", automation.pk)
        return 0


def severity_ok(automation, alert) -> bool:
    if not automation.min_severity:
        return True
    have = _SEVERITY_RANK.get(getattr(alert, "severity", ""), 0)
    need = _SEVERITY_RANK.get(automation.min_severity, 0)
    return have >= need


def tags_ok(automation, host) -> bool:
    if not automation.event_tags:
        return True
    if host is None:
        return False
    want = {str(t).lower() for t in automation.event_tags}
    return bool(want & {str(t).lower() for t in (host.tags or [])})


def handle_event(event_name: str, payload: dict) -> None:
    """Called by the hook subscriptions. Fan every enabled event-automation
    for this event through its filters, then dispatch."""
    from .models import Automation

    host = payload.get("host")
    alert = payload.get("alert")
    if alert is not None and host is None:
        host = getattr(alert, "host", None)

    autos = Automation.objects.filter(
        enabled=True, trigger=Automation.Trigger.EVENT, event=event_name)
    for auto in autos.select_related("task_definition", "target_host", "event_rule"):
        if alert is not None and not severity_ok(auto, alert):
            continue
        # Specific-rule filter: only fire for that exact alert rule.
        if auto.event_rule_id and getattr(alert, "rule_id", None) != auto.event_rule_id:
            continue
        if not tags_ok(auto, host):
            continue
        run_automation(auto, event_host=host)
