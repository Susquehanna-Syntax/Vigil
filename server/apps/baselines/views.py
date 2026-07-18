from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.tasks.models import TaskDefinition

from .models import Baseline, eligible


def _row(b: Baseline) -> dict:
    return {
        "id": str(b.id),
        "definition_id": str(b.definition_id),
        "definition_name": b.definition.name,
        "target_tags": b.target_tags,
        "enabled": b.enabled,
        "created_at": b.created_at.isoformat(),
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_index(request):
    if request.method == "GET":
        rows = Baseline.objects.select_related("definition").order_by("created_at")
        return Response([_row(b) for b in rows])

    definition = get_object_or_404(
        TaskDefinition, pk=request.data.get("definition_id"))
    ok, why = eligible(definition)
    if not ok:
        return Response({"detail": why}, status=400)
    tags = request.data.get("target_tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return Response({"detail": "target_tags must be a list of strings"}, status=400)
    baseline = Baseline.objects.create(
        definition=definition, target_tags=tags, created_by=request.user)
    return Response(_row(baseline), status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_detail(request, baseline_id):
    baseline = get_object_or_404(Baseline, pk=baseline_id)
    if request.method == "DELETE":
        baseline.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if "enabled" in request.data:
        baseline.enabled = bool(request.data["enabled"])
        baseline.save(update_fields=["enabled"])
    return Response(_row(baseline))
