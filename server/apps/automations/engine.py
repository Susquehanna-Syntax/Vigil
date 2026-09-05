"""Dispatch logic shared by event and scheduled automations.

Both paths converge on :func:`run_automation`, which resolves the target
host(s), builds the agent steps from the automation's task or playbook, and
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
    action is unresolvable (deleted definition, unknown playbook, ineligible)."""
    from apps.playbooks.expansion import PlaybookExpandError, expand_actions, _max_risk
    from apps.playbooks.models import Playbook, build_agent_steps

    if automation.action_kind == automation.ActionKind.PLAYBOOK:
        if automation.playbook_id is None:
            logger.warning("automation %s: playbook missing or deleted", automation.pk)
            return None
        playbook = (Playbook.objects
                    .filter(pk=automation.playbook_id)
                    .prefetch_related("steps__definition")
                    .first())
        if playbook is None:
            logger.warning("automation %s: playbook %s no longer exists",
                           automation.pk, automation.playbook_id)
            return None
        try:
            return build_agent_steps(playbook)
        except PlaybookExpandError as exc:
            logger.warning("automation %s: playbook expand failed: %s",
                           automation.pk, exc)
            return None

    definition = automation.task_definition
    if definition is None:
        return None
    spec = definition.parsed_spec or {}
    actions_src = spec.get("actions") or []
    override = automation.params_override or {}
    if override:
        actions_src = [
            {**a, "params": {**(a.get("params") or {}),
                             **override.get(str(idx), {})}}
            for idx, a in enumerate(actions_src)
        ]
    try:
        actions, risk = expand_actions(actions_src)
    except PlaybookExpandError as exc:
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
        # Row ids, not strings — same comparison, encoded once in the row key
        # rather than re-derived at each call site.
        wanted = list(automation.target_tag_rows.values_list("id", flat=True))
        if not wanted:
            return []
        return list(qs.filter(tag_rows__in=wanted).distinct())
    return list(qs)  # ALL


def run_automation(automation, *, event_host=None) -> int:
    """Execute *automation*, returning the number of hosts it dispatched to.
    Never raises — an automation failure must not break the event that fired
    it or the beat loop."""
    from django.utils.timezone import now

    from apps.tasks.models import Task, TaskRun

    try:
        # Wave-by-wave dispatch hands off to the rollout machinery entirely:
        # it does its own host selection from wave tags, its own gating and
        # its own history, so none of the direct path below applies.
        if automation.dispatch_mode == automation.DispatchMode.ROLLOUT:
            return _start_rollout_for(automation)

        built = _steps_for(automation)
        if not built:
            return 0
        steps, risk = built
        if not steps:
            return 0
        hosts = _resolve_hosts(automation, event_host)
        # A monitor-mode host can't execute; skip it silently.
        hosts = [h for h in hosts if getattr(h, "mode", None) != "monitor"]
        if not hosts:
            return 0
        created = 0
        label = (automation.playbook.name if automation.action_kind == "playbook"
                 and automation.playbook
                 else (automation.task_definition.name if automation.task_definition else automation.name))
        # Group the dispatch under a run so it shows up in history as one
        # event rather than a scatter of orphan tasks.
        run = TaskRun.objects.create(
            source=TaskRun.Source.AUTOMATION,
            automation=automation,
            playbook=automation.playbook if automation.action_kind == "playbook" else None,
            definition=automation.task_definition,
            name_snapshot=f"{automation.name} → {label}"[:120],
            requested_by=automation.created_by,
            host_count=len(hosts),
            step_count=len(steps),
        )
        for host in hosts:
            Task.objects.create(
                host=host,
                run=run,
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


def host_ok(automation, host) -> bool:
    """Scope the trigger to one host. Independent of `target`: an automation
    may watch one host and act on another."""
    if not automation.event_host_id:
        return True
    return host is not None and host.pk == automation.event_host_id


def event_text(payload: dict) -> tuple[str, str]:
    """The (name, description) an event carries, whatever kind of event it is.

    This used to read `alert.rule.name` and `alert.message` and nothing else,
    which meant a text filter on any event *other* than ``alert_fired`` was
    silently ignored — the automation fired regardless of what the operator had
    typed. Every event carries something nameable; this finds it.

    Returns two empty strings when the payload holds nothing recognisable,
    which the caller treats as "no match" rather than "matches everything".
    """
    alert = payload.get("alert")
    if alert is not None:
        rule = getattr(alert, "rule", None)
        return (getattr(rule, "name", "") or "",
                getattr(alert, "message", "") or "")

    insight = payload.get("insight")
    if insight is not None:
        return (getattr(insight, "title", "") or "",
                getattr(insight, "body", None) or getattr(insight, "message", "") or "")

    task = payload.get("task")
    if task is not None:
        return (getattr(task, "step_label", "") or getattr(task, "action", "") or "",
                getattr(task, "result_output", "") or "")

    job = payload.get("job")
    if job is not None:
        profile = getattr(job, "profile", None)
        return (getattr(profile, "name", "") or "rebuild",
                getattr(job, "state", "") or "")

    host = payload.get("host")
    if host is not None:
        return (getattr(host, "hostname", "") or "", "")

    return ("", "")


def _apply_operator(mode, needle: str, haystacks: list, choices) -> bool:
    """Run one match operator over the candidate strings.

    Negative operators are true when the text matches *none* of the fields —
    the intuitive reading of "does not contain", and the safe one: a filter
    meant to exclude something must not let it through because it matched the
    field the operator was not thinking about.
    """
    import re as _re

    lowered = [h.lower() for h in haystacks]

    if mode in (choices.REGEX, choices.NOT_REGEX):
        try:
            pattern = _re.compile(needle, _re.IGNORECASE)
        except _re.error:
            # A malformed pattern must not fire the automation for everything.
            # Refusing to match is the safe reading of "I could not evaluate
            # this filter".
            logger.warning("automation regex %r is invalid; treating as no match",
                           needle)
            return False
        found = any(pattern.search(h) for h in haystacks)
        return not found if mode == choices.NOT_REGEX else found

    tests = {
        choices.CONTAINS: lambda h: needle in h,
        choices.NOT_CONTAINS: lambda h: needle in h,
        choices.EQUALS: lambda h: h.strip() == needle,
        choices.NOT_EQUALS: lambda h: h.strip() == needle,
        choices.STARTS_WITH: lambda h: h.lstrip().startswith(needle),
        choices.ENDS_WITH: lambda h: h.rstrip().endswith(needle),
    }
    test = tests.get(mode, tests[choices.CONTAINS])
    found = any(test(h) for h in lowered)
    if mode in (choices.NOT_CONTAINS, choices.NOT_EQUALS):
        return not found
    return found


def text_ok(automation, alert, payload: dict | None = None) -> bool:
    """Match the event's name and/or description against the filter.

    Works for every event, not only alerts — see :func:`event_text`. The
    ``alert`` argument is kept so existing callers and tests keep working; when
    a full payload is supplied it takes precedence.

    Case-insensitive, since nobody filtering on "backup" means to be tripped
    up by "Backup".
    """
    needle = (automation.match_text or "").strip().lower()
    if not needle:
        return True

    if payload is None:
        payload = {"alert": alert} if alert is not None else {}
    name, message = event_text(payload)

    F = automation.MatchField
    haystacks = {F.RULE: [name], F.MESSAGE: [message]}
    haystacks[F.ANY] = [name, message]
    fields = haystacks.get(automation.match_field, haystacks[F.ANY])

    return _apply_operator(automation.match_mode, needle, fields,
                           automation.MatchMode)


def tags_ok(automation, host) -> bool:
    if not automation.event_tags:
        return True
    if host is None:
        return False
    # Same unsaved-instance fallback as Playbook.matches — see the note there.
    if automation._state.adding or host._state.adding:
        return bool({str(t).lower() for t in automation.event_tags}
                    & {str(t).lower() for t in (host.tags or [])})
    want = set(automation.event_tag_rows.values_list("id", flat=True))
    if not want:
        return False
    return bool(want & set(host.tag_rows.values_list("id", flat=True)))


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
    for auto in autos.select_related("task_definition", "target_host",
                                     "event_rule", "event_host"):
        if alert is not None and not severity_ok(auto, alert):
            continue
        # Specific-rule filter: only fire for that exact alert rule.
        if auto.event_rule_id and getattr(alert, "rule_id", None) != auto.event_rule_id:
            continue
        if not host_ok(auto, host):
            continue
        if not text_ok(auto, alert, payload):
            continue
        if not tags_ok(auto, host):
            continue
        run_automation(auto, event_host=host)


def _start_rollout_for(automation) -> int:
    """Start a staged rollout of the automation's task or playbook.

    Returns 1 when a rollout started, 0 otherwise. Never raises into the
    caller: an automation that cannot roll out (no enabled waves, a deleted
    target) must not take down the event bus with it.
    """
    from apps.tasks.rollout import start_rollout

    try:
        if automation.action_kind == automation.ActionKind.PLAYBOOK:
            if automation.playbook is None:
                logger.warning("automation %s: playbook missing, cannot roll out",
                               automation.pk)
                return 0
            start_rollout(playbook=automation.playbook, user=automation.created_by)
        else:
            if automation.task_definition is None:
                logger.warning("automation %s: definition missing, cannot roll out",
                               automation.pk)
                return 0
            start_rollout(automation.task_definition, user=automation.created_by)
    except ValueError as exc:
        logger.warning("automation %s: rollout refused: %s", automation.pk, exc)
        return 0
    return 1
