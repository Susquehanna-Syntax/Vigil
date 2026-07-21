from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.baselines.models import Baseline
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
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
        "cron": {"minute": a.cron_minute, "hour": a.cron_hour, "dom": a.cron_dom,
                 "month": a.cron_month, "dow": a.cron_dow},
        "cron_display": a.cron_display,
        "action_kind": a.action_kind,
        "task_definition": str(a.task_definition_id) if a.task_definition_id else None,
        "task_name": a.task_definition.name if a.task_definition_id else None,
        "baseline_name": a.baseline_name,
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
    cron = data.get("cron") or {}
    for k, field in (("minute", "cron_minute"), ("hour", "cron_hour"),
                     ("dom", "cron_dom"), ("month", "cron_month"), ("dow", "cron_dow")):
        if k in cron:
            setattr(a, field, str(cron[k]).strip() or "*")
    if "action_kind" in data:
        if data["action_kind"] not in Automation.ActionKind.values:
            return "invalid action_kind"
        a.action_kind = data["action_kind"]
    if "task_definition" in data:
        a.task_definition = (TaskDefinition.objects.filter(pk=data["task_definition"]).first()
                             if data["task_definition"] else None)
    if "baseline_name" in data:
        a.baseline_name = (data["baseline_name"] or "").strip()
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
        if not Baseline.objects.filter(name__iexact=a.baseline_name).exists():
            return f"no baseline named {a.baseline_name!r}"
    # A scheduled automation can't target "the event host" — there is no event.
    if a.trigger == Automation.Trigger.SCHEDULE and a.target == Automation.Target.EVENT_HOST:
        return "a scheduled automation needs a concrete target (tags, a host, or all)"
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def automation_index(request):
    if request.method == "GET":
        return Response({
            "automations": [_row(a) for a in Automation.objects.select_related(
                "task_definition", "target_host")],
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
