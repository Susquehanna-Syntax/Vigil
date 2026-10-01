from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin

from .models import AppRule, UpdatePolicy
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
        "app_rules": [{"app": r.app, "source": r.source, "state": r.state,
                       "version": r.version} for r in p.app_rules.all()],
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _save(request, policy: UpdatePolicy, data: dict) -> Response | None:
    """Validate and write *data* onto *policy*; rules are replaced as a whole."""
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
        policy.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if error := _save(request, policy, request.data):
        return error
    return Response(_row(policy))
