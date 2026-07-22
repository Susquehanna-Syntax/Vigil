from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.tasks.models import TaskDefinition

from .models import Baseline, BaselineStep, eligible


def _row(b: Baseline) -> dict:
    return {
        "id": str(b.id),
        "name": b.name,
        "description": b.description,
        "target_tags": b.target_tags,
        "enabled": b.enabled,
        "created_at": b.created_at.isoformat(),
        "steps": [
            {
                "definition_id": str(s.definition_id),
                "definition_name": s.definition.name,
                "risk": s.definition.risk_level,
                "order": s.order,
                "params_override": s.params_override or {},
            }
            for s in b.steps.select_related("definition").order_by("order")
        ],
    }


def _validate_and_set_steps(baseline: Baseline, definition_ids) -> Response | None:
    """Replace the sequence. Entries are bare definition ids or
    ``{"definition_id": ..., "params_override": {...}}`` dicts.
    Returns an error Response or None."""
    from apps.tasks.spec import validate_params_override

    if not isinstance(definition_ids, list) or not definition_ids:
        return Response({"detail": "definition_ids must be a non-empty list"},
                        status=400)
    definitions = []
    for entry in definition_ids:
        override = {}
        did = entry
        if isinstance(entry, dict):
            did = entry.get("definition_id")
            override = entry.get("params_override") or {}
        d = TaskDefinition.objects.filter(pk=did).first()
        if d is None:
            return Response({"detail": f"unknown definition {did}"}, status=400)
        ok, why = eligible(d)
        if not ok:
            return Response({"detail": f"{d.name}: {why}"}, status=400)
        err = validate_params_override(d.parsed_spec or {}, override)
        if err is not None:
            return Response({"detail": f"{d.name}: {err}"}, status=400)
        definitions.append((d, override))
    baseline.steps.all().delete()
    BaselineStep.objects.bulk_create([
        BaselineStep(baseline=baseline, definition=d, order=i,
                     params_override=override)
        for i, (d, override) in enumerate(definitions)
    ])
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_index(request):
    if request.method == "GET":
        rows = Baseline.objects.prefetch_related("steps__definition").order_by("created_at")
        return Response([_row(b) for b in rows])

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=400)
    if Baseline.objects.filter(name__iexact=name).exists():
        return Response({"detail": f"a baseline named {name!r} already exists"},
                        status=400)
    tags = request.data.get("target_tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return Response({"detail": "target_tags must be a list of strings"}, status=400)

    with transaction.atomic():
        baseline = Baseline.objects.create(
            name=name,
            description=(request.data.get("description") or "").strip(),
            target_tags=[t.strip() for t in tags if t.strip()],
            enabled=bool(request.data.get("enabled", True)),
            created_by=request.user,
        )
        err = _validate_and_set_steps(baseline, request.data.get("definition_ids"))
        if err is not None:
            transaction.set_rollback(True)
            return err
    return Response(_row(baseline), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_detail(request, baseline_id):
    baseline = get_object_or_404(Baseline, pk=baseline_id)
    if request.method == "GET":
        return Response(_row(baseline))
    if request.method == "DELETE":
        baseline.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return Response({"detail": "name cannot be empty"}, status=400)
        clash = Baseline.objects.filter(name__iexact=name).exclude(pk=baseline.pk)
        if clash.exists():
            return Response({"detail": f"a baseline named {name!r} already exists"},
                            status=400)
        baseline.name = name
    if "description" in data:
        baseline.description = (data["description"] or "").strip()
    if "enabled" in data:
        baseline.enabled = bool(data["enabled"])
    if "target_tags" in data:
        tags = data["target_tags"] or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return Response({"detail": "target_tags must be a list of strings"},
                            status=400)
        baseline.target_tags = [t.strip() for t in tags if t.strip()]
    with transaction.atomic():
        baseline.save()
        if "definition_ids" in data:
            err = _validate_and_set_steps(baseline, data["definition_ids"])
            if err is not None:
                transaction.set_rollback(True)
                return err
    return Response(_row(baseline))
