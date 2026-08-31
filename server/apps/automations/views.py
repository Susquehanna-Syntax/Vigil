from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.baselines.models import Baseline
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
from vigil import scoping
from vigil.hooks import KNOWN_EVENTS

from .models import Automation
from .tasks import sync_periodic_task

# Events an automation can trigger on, with human labels.
EVENT_LABELS = {
    "alert_fired": "An alert fires",
    "host_approved": "A host is approved",
    "host_rejected": "A host is rejected",
    "task_completed": "A task completes",
    "insight_created": "An insight is created",
}


def _row(a: Automation) -> dict:
    return {
        "id": str(a.id), "name": a.name, "enabled": a.enabled,
        "trigger": a.trigger,
        "event": a.event, "min_severity": a.min_severity, "event_tags": a.event_tags,
        "event_rule": str(a.event_rule_id) if a.event_rule_id else None,
        "event_rule_name": a.event_rule.name if a.event_rule_id else None,
        "event_host": str(a.event_host_id) if a.event_host_id else None,
        "event_host_name": a.event_host.hostname if a.event_host_id else None,
        "match_text": a.match_text,
        "match_field": a.match_field,
        "match_mode": a.match_mode,
        "cron": {"minute": a.cron_minute, "hour": a.cron_hour, "dom": a.cron_dom,
                 "month": a.cron_month, "dow": a.cron_dow},
        "cron_display": a.cron_display,
        "action_kind": a.action_kind,
        "dispatch_mode": a.dispatch_mode,
        "task_definition": str(a.task_definition_id) if a.task_definition_id else None,
        "task_name": a.task_definition.name if a.task_definition_id else None,
        # Still a name in the JSON: the UI works in names, and the FK is an
        # internal correctness concern.
        "baseline_name": a.baseline.name if a.baseline_id else "",
        "params_override": a.params_override or {},
        "target": a.target, "target_tags": a.target_tags,
        "target_host": str(a.target_host_id) if a.target_host_id else None,
        "last_run": a.last_run.isoformat() if a.last_run else None,
        "run_count": a.run_count,
    }


def _apply(a: Automation, data) -> str | None:
    """Set fields from *data*; returns an error string or None."""
    if "name" in data:
        a.name = (data["name"] or "").strip()
    if "enabled" in data:
        a.enabled = bool(data["enabled"])
    if "trigger" in data:
        if data["trigger"] not in Automation.Trigger.values:
            return "invalid trigger"
        a.trigger = data["trigger"]
    if "event" in data:
        if data["event"] and data["event"] not in KNOWN_EVENTS:
            return f"unknown event {data['event']!r}"
        a.event = data["event"] or ""
    if "min_severity" in data:
        a.min_severity = data["min_severity"] or ""
    if "event_rule" in data:
        from apps.alerts.models import AlertRule
        a.event_rule = (AlertRule.objects.filter(pk=data["event_rule"]).first()
                        if data["event_rule"] else None)
    if "event_tags" in data:
        a.event_tags = [t.strip() for t in (data["event_tags"] or []) if t.strip()]
    if "event_host" in data:
        from apps.hosts.models import Host
        a.event_host = (Host.objects.filter(pk=data["event_host"]).first()
                        if data["event_host"] else None)
    if "match_text" in data:
        a.match_text = (data["match_text"] or "").strip()[:200]
    if "match_field" in data:
        if data["match_field"] not in Automation.MatchField.values:
            return "invalid match_field"
        a.match_field = data["match_field"]
    if "match_mode" in data:
        if data["match_mode"] not in Automation.MatchMode.values:
            return "invalid match_mode"
        a.match_mode = data["match_mode"]
    cron = data.get("cron") or {}
    for k, field in (("minute", "cron_minute"), ("hour", "cron_hour"),
                     ("dom", "cron_dom"), ("month", "cron_month"), ("dow", "cron_dow")):
        if k in cron:
            setattr(a, field, str(cron[k]).strip() or "*")
    if "dispatch_mode" in data:
        if data["dispatch_mode"] not in Automation.DispatchMode.values:
            return "dispatch_mode must be 'direct' or 'rollout'"
        a.dispatch_mode = data["dispatch_mode"]
    if "action_kind" in data:
        if data["action_kind"] not in Automation.ActionKind.values:
            return "invalid action_kind"
        a.action_kind = data["action_kind"]
    if "task_definition" in data:
        a.task_definition = (TaskDefinition.objects.filter(pk=data["task_definition"]).first()
                             if data["task_definition"] else None)
    wanted_baseline = None
    if "baseline_name" in data:
        wanted_baseline = (data["baseline_name"] or "").strip()
        a.baseline = (Baseline.objects.filter(name__iexact=wanted_baseline).first()
                      if wanted_baseline else None)
    if "params_override" in data:
        a.params_override = data["params_override"] or {}
    if "target" in data:
        if data["target"] not in Automation.Target.values:
            return "invalid target"
        a.target = data["target"]
    if "target_tags" in data:
        a.target_tags = [t.strip() for t in (data["target_tags"] or []) if t.strip()]
    if "target_host" in data:
        a.target_host = (Host.objects.filter(pk=data["target_host"]).first()
                         if data["target_host"] else None)

    if not a.name:
        return "name is required"
    if a.trigger == Automation.Trigger.EVENT and not a.event:
        return "an event trigger needs an event"
    if a.action_kind == Automation.ActionKind.TASK and not a.task_definition_id:
        return "pick a task definition"
    if a.action_kind == Automation.ActionKind.BASELINE:
        if a.baseline_id is None:
            return (f"no baseline named {wanted_baseline!r}" if wanted_baseline
                    else "pick a baseline")
        a.params_override = {}  # per-step inputs live on the baseline itself
    elif a.params_override and a.task_definition_id:
        from apps.tasks.spec import validate_params_override
        err = validate_params_override(
            a.task_definition.parsed_spec or {}, a.params_override)
        if err is not None:
            return err
    # A scheduled automation can't target "the event host" — there is no event.
    if a.trigger == Automation.Trigger.SCHEDULE and a.target == Automation.Target.EVENT_HOST:
        return "a scheduled automation needs a concrete target (tags, a host, or all)"
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_index(request):
    if request.method == "GET":
        return Response({
            "automations": [_row(a) for a in scoping.filter_by_site(
                Automation.objects.select_related("task_definition", "target_host"),
                request.user, cascade_global=True)],
            "events": EVENT_LABELS,
        })
    a = Automation(created_by=request.user, trigger=request.data.get("trigger", "event"),
                   action_kind=request.data.get("action_kind", "task"))
    err = _apply(a, request.data)
    if err:
        return Response({"detail": err}, status=400)
    a.save()
    sync_periodic_task(a)
    return Response(_row(a), status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_detail(request, automation_id):
    a = get_object_or_404(Automation, pk=automation_id)
    if request.method == "DELETE":
        from .tasks import _disable_task
        _disable_task(a)
        a.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    err = _apply(a, request.data)
    if err:
        return Response({"detail": err}, status=400)
    a.save()
    sync_periodic_task(a)
    return Response(_row(a))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_run_now(request, automation_id):
    """Fire a scheduled/event automation on demand (test button)."""
    from .engine import run_automation

    a = get_object_or_404(Automation, pk=automation_id)
    n = run_automation(a)
    return Response({"dispatched": n})


# ── Community YAML ───────────────────────────────────────────────────────────
#
# Import funnels the parsed document through _apply, the same setter the JSON
# API uses, rather than assigning fields directly. That keeps one set of rules
# about what a valid automation is — the event must be known, a scheduled one
# cannot target the event host, a baseline action cannot carry per-task
# overrides — instead of a second, quietly diverging set for YAML.


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_yaml(request, automation_id):
    from datetime import date

    from vigil.contentyaml import ContentYamlError, slugify

    from .community_yaml import to_yaml

    automation = get_object_or_404(
        scoping.filter_by_site(
            Automation.objects.select_related("task_definition", "baseline"),
            request.user, cascade_global=True),
        pk=automation_id)
    author = (request.user.get_full_name() or "").strip() or request.user.username
    try:
        text = to_yaml(automation, author=author, created=date.today())
    except ContentYamlError as exc:
        # The unshareable cases — a specific target host, a specific watched
        # host — are a 400 with the reason, not a 500. The message tells the
        # operator what to change to make it shareable.
        return Response({"detail": str(exc)}, status=400)
    return Response({
        "yaml": text,
        "filename": f"{slugify(automation.name, fallback='automation')}.yaml",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_from_yaml(request):
    """Create or replace an automation from community YAML."""
    from vigil.contentyaml import ContentYamlError

    from .community_yaml import parse, resolve_action

    try:
        parsed = parse(request.data.get("yaml") or "")
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    definitions = scoping.filter_by_site(
        TaskDefinition.objects.all(), request.user, cascade_global=True)
    baselines = scoping.filter_by_site(
        Baseline.objects.all(), request.user, cascade_global=True)
    from apps.tasks.views import community_names_by_slug

    try:
        definition, baseline = resolve_action(
            parsed, definitions=definitions, baselines=baselines,
            task_names_by_slug=community_names_by_slug("tasks"),
            baseline_names_by_slug=community_names_by_slug("baselines"))
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    automation_id = request.data.get("automation_id")
    automation = None
    if automation_id:
        automation = get_object_or_404(
            scoping.filter_by_site(Automation.objects.all(), request.user,
                                   cascade_global=True),
            pk=automation_id)
    if automation is None:
        automation = Automation(created_by=request.user)

    data = {
        "name": parsed["name"],
        "enabled": parsed["enabled"],
        "trigger": parsed["trigger"],
        "event": parsed["event"],
        "min_severity": parsed["min_severity"],
        "event_tags": parsed["event_tags"],
        "match_text": parsed["match_text"],
        "match_field": parsed["match_field"],
        "match_mode": parsed["match_mode"],
        "cron": parsed["cron"],
        "action_kind": parsed["action_kind"],
        "params_override": parsed["params_override"],
        "target": parsed["target"],
        "target_tags": parsed["target_tags"],
        # Shared files never name a specific machine, so an import must clear
        # any host pinned on the automation it is replacing. Leaving a stale
        # host id behind would silently keep targeting it.
        "target_host": None,
        "event_host": None,
    }
    if parsed["action_kind"] == "task":
        data["task_definition"] = str(definition.id)
        data["baseline_name"] = ""
    else:
        data["task_definition"] = None
        data["baseline_name"] = baseline.name

    created = automation._state.adding
    if err := _apply(automation, data):
        return Response({"detail": err}, status=400)
    # Keep the catalog's identity, for the same reason baselines do.
    if parsed.get("uid"):
        automation.community_uid = parsed["uid"]
    automation.save()
    sync_periodic_task(automation)
    return Response(_row(automation),
                    status=status.HTTP_201_CREATED if created
                    else status.HTTP_200_OK)
