from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.tasks.models import TaskDefinition
from vigil import scoping

from .models import Baseline, BaselineStep, eligible


def _row(b: Baseline) -> dict:
    return {
        "id": str(b.id),
        "name": b.name,
        "description": b.description,
        "target_tags": b.target_tags,
        "enabled": b.enabled,
        "allow_high_risk": b.allow_high_risk,
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


def _high_risk_gate(request, baseline: Baseline, requested) -> Response | None:
    """Authorize a change to *baseline*'s allow_high_risk flag.

    Turning it ON costs a fresh TOTP code. That one confirmation authorizes
    every future unattended dispatch of this baseline's high-risk steps, so
    it is the same class of act as deploying a high-risk task by hand — and
    the only moment a human is present to make it. Turning it OFF is
    unguarded: removing an authorization needs no authorization.
    """
    if requested is None or bool(requested) == baseline.allow_high_risk:
        return None
    if not requested:
        baseline.allow_high_risk = False
        return None

    from apps.accounts.totp import require_totp_confirmation

    if error := require_totp_confirmation(request.user, request.data):
        return Response(
            {"detail": f"Allowing high-risk steps needs confirmation: {error}",
             "needs_totp": True},
            status=403)
    baseline.allow_high_risk = True
    return None


def _validate_and_set_steps(baseline: Baseline, definition_ids) -> Response | None:
    """Replace the sequence. Entries are bare definition ids or
    ``{"definition_id": ..., "params_override": {...}}`` dicts.
    Returns an error Response or None.

    Eligibility is judged against the baseline's own allow_high_risk flag, so
    callers must set it before calling — otherwise a baseline created with
    the flag on in the same request would still reject its high-risk steps.
    """
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
        ok, why = eligible(d, allow_high_risk=baseline.allow_high_risk)
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
        rows = scoping.filter_by_site(
            Baseline.objects.prefetch_related("steps__definition"),
            request.user, cascade_global=True).order_by("created_at")
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
        # Before the steps are validated: eligibility is judged against this
        # flag, so a baseline created with high-risk steps and the box ticked
        # would otherwise reject its own steps.
        if err := _high_risk_gate(request, baseline, request.data.get("allow_high_risk")):
            transaction.set_rollback(True)
            return err
        baseline.save(update_fields=["allow_high_risk"])
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
    if err := _high_risk_gate(request, baseline, data.get("allow_high_risk")):
        return err
    with transaction.atomic():
        baseline.save()
        if "definition_ids" in data:
            err = _validate_and_set_steps(baseline, data["definition_ids"])
            if err is not None:
                transaction.set_rollback(True)
                return err
    return Response(_row(baseline))


# ── Community YAML ───────────────────────────────────────────────────────────
#
# Export and import a baseline in the dialect the community repo speaks, so a
# baseline can be shared the same way a task already could. Import routes
# through _validate_and_set_steps and _high_risk_gate rather than writing rows
# directly: a YAML file must not be a way around the eligibility rules or the
# TOTP confirmation that guards allow_high_risk.


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_yaml(request, baseline_id):
    """The baseline as community YAML, plus the filename it should be saved as."""
    from datetime import date

    from vigil.contentyaml import ContentYamlError, slugify

    from .community_yaml import to_yaml

    baseline = get_object_or_404(
        scoping.filter_by_site(Baseline.objects.all(), request.user,
                               cascade_global=True),
        pk=baseline_id)
    author = (request.user.get_full_name() or "").strip() or request.user.username
    try:
        text = to_yaml(baseline, author=author, created=date.today())
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response({
        "yaml": text,
        "filename": f"{slugify(baseline.name, fallback='baseline')}.yaml",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def baseline_from_yaml(request):
    """Create or replace a baseline from community YAML.

    ``baseline_id`` in the body updates that baseline in place; without it a
    new one is created. Importing over an existing baseline is how Fork →
    edit → re-import works without accumulating duplicates.
    """
    from vigil.contentyaml import ContentYamlError

    from .community_yaml import parse, resolve_steps

    try:
        parsed = parse(request.data.get("yaml") or "")
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    from apps.tasks.views import community_names_by_slug

    visible_defs = scoping.filter_by_site(
        TaskDefinition.objects.all(), request.user, cascade_global=True)
    try:
        steps = resolve_steps(parsed["steps"], visible_defs,
                              community_names_by_slug("tasks"))
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)
    steps.sort(key=lambda s: s["order"])

    baseline_id = request.data.get("baseline_id")
    existing = None
    if baseline_id:
        existing = get_object_or_404(
            scoping.filter_by_site(Baseline.objects.all(), request.user,
                                   cascade_global=True),
            pk=baseline_id)

    clash = Baseline.objects.filter(name__iexact=parsed["name"])
    if existing is not None:
        clash = clash.exclude(pk=existing.pk)
    if clash.exists():
        return Response(
            {"detail": f"a baseline named {parsed['name']!r} already exists"},
            status=400)

    with transaction.atomic():
        baseline = existing or Baseline(created_by=request.user)
        baseline.name = parsed["name"]
        baseline.description = parsed["description"]
        baseline.target_tags = parsed["target_tags"]
        # Keep the catalog's identity for this baseline. Without it a second
        # fork cannot tell it already has this one, and tries to import it
        # again — which fails on the duplicate name and reads as an error when
        # nothing is wrong.
        if parsed.get("uid"):
            baseline.community_uid = parsed["uid"]
        if existing is None:
            baseline.save()
        # The flag is a 2FA-guarded act whichever door it comes through. A
        # YAML file asking for high-risk steps has to pass the same gate a
        # checkbox does, or importing would be the way around it.
        if err := _high_risk_gate(request, baseline, parsed["allow_high_risk"]):
            transaction.set_rollback(True)
            return err
        baseline.save()
        err = _validate_and_set_steps(
            baseline,
            [{"definition_id": str(s["definition"].id),
              "params_override": s["params_override"]} for s in steps])
        if err is not None:
            transaction.set_rollback(True)
            return err

    return Response(_row(baseline),
                    status=status.HTTP_200_OK if existing
                    else status.HTTP_201_CREATED)
