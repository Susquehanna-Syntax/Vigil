from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin

from .compile import compile_policy
from .drift import policy_drift
from .models import AppRule, PolicyChange, UpdatePolicy
from .tasks import delete_periodic_task, sync_periodic_task
from .validation import PolicyError, apply_fields, clean_rules


def _row(p: UpdatePolicy) -> dict:
    return {
        "id": str(p.id), "name": p.name, "enabled": p.enabled,
        "target_tags": p.target_tags or [],
        "site_id": str(p.site_id) if p.site_id else None,
        "cron_minute": p.cron_minute, "cron_hour": p.cron_hour,
        "cron_dow": p.cron_dow, "window_hours": p.window_hours,
        "wave_group_tag": p.wave_group_tag, "approval_mode": p.approval_mode,
        "patch_enabled": p.patch_enabled,
        "windows_classifications": p.windows_classifications or [],
        "deferral_days": p.deferral_days, "reboot": p.reboot,
        "linux_updates": p.linux_updates,
        "allow_high_risk": p.allow_high_risk,
        "high_risk": bool(p.task_definition_id and p.task_definition.risk_level == "high"),
        "task_definition": str(p.task_definition_id) if p.task_definition_id else None,
        "app_rules": [{"app": r.app, "source": r.source, "state": r.state,
                       "version": r.version} for r in p.app_rules.all()],
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _high_risk_gate(request, policy: UpdatePolicy, requested) -> Response | None:
    """Authorize a change to allow_high_risk — the same gate automations use.

    Turning it on costs a fresh TOTP code, and that confirmation authorizes
    every unattended reboot this policy's window will run. Turning it off
    needs nothing.
    """
    if requested is None or bool(requested) == policy.allow_high_risk:
        return None
    if not requested:
        policy.allow_high_risk = False
        return None
    from apps.accounts.totp import require_totp_confirmation

    if error := require_totp_confirmation(request.user, request.data):
        return Response(
            {"detail": f"Allowing high-risk steps needs confirmation: {error}",
             "needs_totp": True}, status=status.HTTP_401_UNAUTHORIZED)
    policy.allow_high_risk = True
    return None


def _save(request, policy: UpdatePolicy, data: dict) -> Response | None:
    """Validate and write *data* onto *policy*; rules are replaced as a whole."""
    if error := _high_risk_gate(request, policy, data.get("allow_high_risk")):
        return error
    try:
        apply_fields(policy, data)
        rules = clean_rules(data.get("app_rules")) if "app_rules" in data else None
    except PolicyError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        if policy.created_by_id is None and policy._state.adding:
            policy.created_by = request.user
        policy.save()
        if rules is not None:
            policy.app_rules.all().delete()
            AppRule.objects.bulk_create([AppRule(policy=policy, **r) for r in rules])
        compile_policy(policy)
        sync_periodic_task(policy)
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def policy_index(request):
    if request.method == "GET":
        qs = UpdatePolicy.objects.prefetch_related("app_rules")
        return Response({"results": [_row(p) for p in qs]})
    policy = UpdatePolicy()
    if error := _save(request, policy, request.data):
        return error
    return Response(_row(policy), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def policy_detail(request, policy_id):
    policy = get_object_or_404(UpdatePolicy, pk=policy_id)
    if request.method == "GET":
        return Response(_row(policy))
    if request.method == "DELETE":
        delete_periodic_task(policy)
        if policy.task_definition_id:
            # Archived, not deleted: run history still points at it.
            from django.utils.timezone import now
            policy.task_definition.archived_at = now()
            policy.task_definition.save(update_fields=["archived_at"])
        policy.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if error := _save(request, policy, request.data):
        return error
    return Response(_row(policy))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def policy_drift_view(request, policy_id):
    """What a run would change right now, host by host."""
    policy = get_object_or_404(UpdatePolicy, pk=policy_id)
    return Response(policy_drift(policy))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def policy_run_now(request, policy_id):
    """Run the policy now rather than at its window — TOTP, like a deploy."""
    from apps.accounts.totp import require_totp_confirmation

    from .run import run_policy

    policy = get_object_or_404(UpdatePolicy, pk=policy_id)
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"Running a policy needs confirmation: {error}",
                         "needs_totp": True}, status=status.HTTP_401_UNAUTHORIZED)
    return Response(run_policy(policy, user=request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def change_list(request):
    qs = PolicyChange.objects.select_related("policy", "host", "decided_by")
    state = request.query_params.get("state")
    if state:
        qs = qs.filter(state=state)
    if policy_id := request.query_params.get("policy"):
        qs = qs.filter(policy_id=policy_id)
    return Response({"results": [{
        "id": str(c.id), "policy_id": str(c.policy_id), "policy": c.policy.name,
        "host_id": str(c.host_id), "hostname": c.host.hostname,
        "changes": c.changes, "state": c.state,
        "created_at": c.created_at.isoformat(),
        "decided_by": c.decided_by.username if c.decided_by else None,
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
    } for c in qs[:500]]})


def _ids(request):
    ids = request.data.get("ids")
    if not isinstance(ids, list) or not ids:
        return None
    return [str(i) for i in ids]


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def change_approve(request):
    """Approve pending changes and dispatch them — TOTP, like a deploy."""
    from apps.accounts.totp import require_totp_confirmation

    from .approval import decide

    ids = _ids(request)
    if ids is None:
        return Response({"detail": "ids must be a non-empty list"},
                        status=status.HTTP_400_BAD_REQUEST)
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"Approving changes needs confirmation: {error}",
                         "needs_totp": True}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        return Response(decide(ids, approve=True, user=request.user))
    except (ValueError, ValidationError):
        return Response({"detail": "ids must be change ids"},
                        status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def change_reject(request):
    from .approval import decide

    ids = _ids(request)
    if ids is None:
        return Response({"detail": "ids must be a non-empty list"},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        return Response(decide(ids, approve=False, user=request.user))
    except (ValueError, ValidationError):
        return Response({"detail": "ids must be change ids"},
                        status=status.HTTP_400_BAD_REQUEST)
