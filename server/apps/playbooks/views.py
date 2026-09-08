from django.db import transaction
from django.utils.timezone import now
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.tasks.models import TaskDefinition
from vigil import scoping

from .models import (Playbook, PlaybookStep, dispatch_to_host, eligible,
                     failing_counts, hosts_failing)


def _row(b: Playbook, *, failing: int | None = None) -> dict:
    return {
        "id": str(b.id),
        "name": b.name,
        "description": b.description,
        "target_tags": b.target_tags,
        "auto_enroll": b.auto_enroll,
        "archived_at": b.archived_at.isoformat() if b.archived_at else None,
        "completion_tag": b.completion_tag,
        "allow_high_risk": b.allow_high_risk,
        "created_at": b.created_at.isoformat(),
        # How many hosts auto-enrolment has stopped trying, because the last
        # run failed there. Zero is the ordinary answer and the card says
        # nothing; anything else is the thing on this page worth reading.
        "failing_hosts": (failing if failing is not None
                          else len(hosts_failing(b))),
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


def _high_risk_gate(request, playbook: Playbook, requested) -> Response | None:
    """Authorize a change to *playbook*'s allow_high_risk flag.

    Turning it ON costs a fresh TOTP code. That one confirmation authorizes
    every future unattended dispatch of this playbook's high-risk steps, so
    it is the same class of act as deploying a high-risk task by hand — and
    the only moment a human is present to make it. Turning it OFF is
    unguarded: removing an authorization needs no authorization.
    """
    if requested is None or bool(requested) == playbook.allow_high_risk:
        return None
    if not requested:
        playbook.allow_high_risk = False
        return None

    from apps.accounts.totp import require_totp_confirmation

    if error := require_totp_confirmation(request.user, request.data):
        return Response(
            {"detail": f"Allowing high-risk steps needs confirmation: {error}",
             "needs_totp": True},
            status=403)
    playbook.allow_high_risk = True
    return None


def _validate_and_set_steps(playbook: Playbook, definition_ids) -> Response | None:
    """Replace the sequence. Entries are bare definition ids or
    ``{"definition_id": ..., "params_override": {...}}`` dicts.
    Returns an error Response or None.

    Eligibility is judged against the playbook's own allow_high_risk flag, so
    callers must set it before calling — otherwise a playbook created with
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
        ok, why = eligible(d, allow_high_risk=playbook.allow_high_risk)
        if not ok:
            return Response({"detail": f"{d.name}: {why}"}, status=400)
        err = validate_params_override(d.parsed_spec or {}, override)
        if err is not None:
            return Response({"detail": f"{d.name}: {err}"}, status=400)
        definitions.append((d, override))
    playbook.steps.all().delete()
    PlaybookStep.objects.bulk_create([
        PlaybookStep(playbook=playbook, definition=d, order=i,
                     params_override=override)
        for i, (d, override) in enumerate(definitions)
    ])
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_index(request):
    if request.method == "GET":
        qs = Playbook.objects.prefetch_related("steps__definition")
        if request.query_params.get("archived") == "1":
            qs = qs.filter(archived_at__isnull=False)
        else:
            qs = qs.filter(archived_at__isnull=True)
        rows = list(scoping.filter_by_site(
            qs, request.user, cascade_global=True).order_by("created_at"))
        counts = failing_counts(rows)
        return Response([_row(b, failing=counts.get(b.id, 0)) for b in rows])

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=400)
    if Playbook.objects.filter(name__iexact=name).exists():
        return Response({"detail": f"a playbook named {name!r} already exists"},
                        status=400)
    tags = request.data.get("target_tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return Response({"detail": "target_tags must be a list of strings"}, status=400)

    with transaction.atomic():
        playbook = Playbook.objects.create(
            name=name,
            description=(request.data.get("description") or "").strip(),
            target_tags=[t.strip() for t in tags if t.strip()],
            completion_tag=(request.data.get("completion_tag") or "").strip(),
            # Never on at creation. Turning it on is a separate, deliberate
            # act that the UI guards with its own confirmation, because it
            # dispatches to every matching host the moment it is set.
            auto_enroll=False,
            created_by=request.user,
        )
        # Before the steps are validated: eligibility is judged against this
        # flag, so a playbook created with high-risk steps and the box ticked
        # would otherwise reject its own steps.
        if err := _high_risk_gate(request, playbook, request.data.get("allow_high_risk")):
            transaction.set_rollback(True)
            return err
        playbook.save(update_fields=["allow_high_risk"])
        err = _validate_and_set_steps(playbook, request.data.get("definition_ids"))
        if err is not None:
            transaction.set_rollback(True)
            return err
    return Response(_row(playbook), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_detail(request, playbook_id):
    playbook = get_object_or_404(Playbook, pk=playbook_id)
    if request.method == "GET":
        return Response(_row(playbook))
    if request.method == "DELETE":
        playbook.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return Response({"detail": "name cannot be empty"}, status=400)
        clash = Playbook.objects.filter(name__iexact=name).exclude(pk=playbook.pk)
        if clash.exists():
            return Response({"detail": f"a playbook named {name!r} already exists"},
                            status=400)
        playbook.name = name
    if "description" in data:
        playbook.description = (data["description"] or "").strip()
    if "completion_tag" in data:
        playbook.completion_tag = (data["completion_tag"] or "").strip()
    if "auto_enroll" in data:
        want = bool(data["auto_enroll"])
        # A playbook that auto-enrols with no completion tag never finishes:
        # nothing marks a host done, so every reconcile pass dispatches it
        # again to the whole target set. Refuse rather than ship that.
        if want and not (playbook.completion_tag or "").strip():
            return Response(
                {"detail": "auto_enroll needs a completion_tag — without one "
                           "the playbook re-runs on every matching host "
                           "forever."},
                status=400)
        playbook.auto_enroll = want
    if "target_tags" in data:
        tags = data["target_tags"] or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return Response({"detail": "target_tags must be a list of strings"},
                            status=400)
        playbook.target_tags = [t.strip() for t in tags if t.strip()]
    if err := _high_risk_gate(request, playbook, data.get("allow_high_risk")):
        return err
    with transaction.atomic():
        playbook.save()
        if "definition_ids" in data:
            err = _validate_and_set_steps(playbook, data["definition_ids"])
            if err is not None:
                transaction.set_rollback(True)
                return err
    return Response(_row(playbook))


# ── Community YAML ───────────────────────────────────────────────────────────
#
# Export and import a playbook in the dialect the community repo speaks, so a
# playbook can be shared the same way a task already could. Import routes
# through _validate_and_set_steps and _high_risk_gate rather than writing rows
# directly: a YAML file must not be a way around the eligibility rules or the
# TOTP confirmation that guards allow_high_risk.


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_yaml(request, playbook_id):
    """The playbook as community YAML, plus the filename it should be saved as."""
    from datetime import date

    from vigil.contentyaml import ContentYamlError, slugify

    from .community_yaml import to_yaml

    playbook = get_object_or_404(
        scoping.filter_by_site(Playbook.objects.all(), request.user,
                               cascade_global=True),
        pk=playbook_id)
    author = (request.user.get_full_name() or "").strip() or request.user.username
    try:
        text = to_yaml(playbook, author=author, created=date.today())
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response({
        "yaml": text,
        "filename": f"{slugify(playbook.name, fallback='playbook')}.yaml",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_from_yaml(request):
    """Create or replace a playbook from community YAML.

    ``playbook_id`` in the body updates that playbook in place; without it a
    new one is created. Importing over an existing playbook is how Fork →
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

    playbook_id = request.data.get("playbook_id")
    existing = None
    if playbook_id:
        existing = get_object_or_404(
            scoping.filter_by_site(Playbook.objects.all(), request.user,
                                   cascade_global=True),
            pk=playbook_id)

    clash = Playbook.objects.filter(name__iexact=parsed["name"])
    if existing is not None:
        clash = clash.exclude(pk=existing.pk)
    if clash.exists():
        return Response(
            {"detail": f"a playbook named {parsed['name']!r} already exists"},
            status=400)

    with transaction.atomic():
        playbook = existing or Playbook(created_by=request.user)
        playbook.name = parsed["name"]
        playbook.description = parsed["description"]
        playbook.target_tags = parsed["target_tags"]
        # Keep the catalog's identity for this playbook. Without it a second
        # fork cannot tell it already has this one, and tries to import it
        # again — which fails on the duplicate name and reads as an error when
        # nothing is wrong.
        if parsed.get("uid"):
            playbook.community_uid = parsed["uid"]
        if existing is None:
            playbook.save()
        # The flag is a 2FA-guarded act whichever door it comes through. A
        # YAML file asking for high-risk steps has to pass the same gate a
        # checkbox does, or importing would be the way around it.
        if err := _high_risk_gate(request, playbook, parsed["allow_high_risk"]):
            transaction.set_rollback(True)
            return err
        playbook.save()
        err = _validate_and_set_steps(
            playbook,
            [{"definition_id": str(s["definition"].id),
              "params_override": s["params_override"]} for s in steps])
        if err is not None:
            transaction.set_rollback(True)
            return err

    return Response(_row(playbook),
                    status=status.HTTP_200_OK if existing
                    else status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_archive(request, playbook_id):
    """Archive or restore a playbook.

    An archived playbook stops auto-enrolling and disappears from lists and
    pickers, but anything already referencing it keeps working and its run
    history stays intact.
    """
    playbook = get_object_or_404(Playbook, pk=playbook_id)
    restore = bool(request.data.get("restore"))
    playbook.archived_at = None if restore else now()
    if not restore:
        # An archived playbook that still auto-enrolled would keep dispatching
        # from a list nobody can see any more.
        playbook.auto_enroll = False
    playbook.save(update_fields=["archived_at", "auto_enroll"])
    return Response(_row(playbook))


def _last_failure(playbook, host) -> dict:
    """What went wrong the last time *playbook* ran on *host*.

    Read from the task rows rather than stored on the playbook: the run
    history is already the record, and a summary kept beside it would be one
    more thing that can disagree with it.
    """
    from apps.tasks.models import Task

    task = (Task.objects
            .filter(run__playbook=playbook, host=host)
            .order_by("-run__created_at", "-created_at")
            .first())
    if task is None:
        return {}
    return {
        "state": task.state,
        "step_label": task.step_label,
        "output": (task.result_output or "")[-4000:],
        "at": task.completed_at.isoformat() if task.completed_at else None,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_failures(request, playbook_id):
    """The hosts auto-enrolment has stopped trying, and why.

    A quarantine nobody can see is just a playbook that silently stopped
    working, which is the failure mode this replaced.
    """
    playbook = get_object_or_404(Playbook, pk=playbook_id)
    return Response([
        {"host_id": str(host.id), "hostname": host.hostname,
         "status": host.status, **_last_failure(playbook, host)}
        for host in hosts_failing(playbook)
    ])


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_retry(request, playbook_id):
    """Dispatch *playbook* again to the hosts it is held back on.

    Pass ``host`` to retry one, or nothing to retry all of them. No TOTP: the
    authorization for an unattended dispatch of this playbook was given when
    auto-enrolment was turned on, and a retry is that same dispatch, to a host
    it already targets. Retrying is also the only way out of a quarantine, so
    guarding it harder than the thing that created it would be backwards.

    The new run is what clears the quarantine — nothing is erased, the newer
    attempt simply outranks the older one.
    """
    playbook = get_object_or_404(Playbook, pk=playbook_id)
    if playbook.archived_at is not None:
        return Response({"detail": "an archived playbook does not dispatch"},
                        status=400)

    wanted = request.data.get("host")
    targets = hosts_failing(playbook)
    if wanted:
        targets = [h for h in targets if str(h.id) == str(wanted)]
        if not targets:
            return Response(
                {"detail": "that host is not being held back by this playbook"},
                status=404)

    dispatched = sum(dispatch_to_host(h, playbooks=[playbook]) for h in targets)
    return Response({"dispatched": dispatched,
                     "hosts": [h.hostname for h in targets]})
