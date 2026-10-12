from pathlib import Path

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
        "inputs": b.inputs or [],
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
                # ``inputs`` here is the step's playbook-input mapping
                # (task input id → text with playbook references);
                # ``params_override`` is the per-action param override.
                "params_override": s.params_override or {},
                "inputs": s.inputs or {},
                "on_not_applicable": s.on_not_applicable,
                "on_failure": s.on_failure,
                "color": s.color,
                "outcome": s.outcome,
                "step_id": s.step_id,
                "branch": s.branch,
            }
            for s in b.steps.select_related("definition").order_by("order")
        ],
        # The if/then/else structure over step ids, or None for a plain
        # ordered playbook (M6 08b).
        "flow": b.flow,
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


#: The colours an author can give a playbook step — Vigil's own palette.
STEP_COLORS = {"", "rose", "lavender", "mint", "peach", "sky", "lemon"}


def _validate_and_set_steps(playbook: Playbook, definition_ids) -> Response | None:
    """Replace the steps. *definition_ids* is the authored tree: each entry
    is a bare definition id, a step dict ``{"definition_id", "id"?,
    "params_override"?, "on_not_applicable"?}``, or a branch
    ``{"if": <condition>, "then": [...], "else": [...]}`` (M6 08b). A flat
    list of steps is simply a tree without branches.
    Returns an error Response or None.

    Eligibility is judged against the playbook's own allow_high_risk flag, so
    callers must set it before calling — otherwise a playbook created with
    the flag on in the same request would still reject its high-risk steps.
    ``playbook.inputs`` must be set before calling too: a step's input
    mapping is checked against the playbook's declarations.
    """
    from apps.tasks.spec import validate_params_override

    from .flow import FlowError, build_flow
    from .inputs import validate_step_inputs

    if not isinstance(definition_ids, list) or not definition_ids:
        return Response({"detail": "definition_ids must be a non-empty list"},
                        status=400)

    def load(entries) -> list:
        tree = []
        for entry in entries:
            if isinstance(entry, dict) and "if" in entry:
                then_nodes = entry.get("then")
                else_nodes = entry.get("else") or []
                if not isinstance(then_nodes, list) or not isinstance(else_nodes, list):
                    raise FlowError("a branch's then/else must be lists")
                tree.append({**entry, "then": load(then_nodes), "else": load(else_nodes)})
                continue
            override, on_not_applicable, on_failure, step_id, did = {}, "stop", "stop", None, entry
            step_inputs_mapping: dict = {}
            color = ""
            outcome = ""
            if isinstance(entry, dict):
                did = entry.get("definition_id")
                override = entry.get("params_override") or {}
                step_inputs_mapping = entry.get("inputs") or {}
                on_not_applicable = entry.get("on_not_applicable") or "stop"
                step_id = entry.get("id")
                if on_not_applicable not in ("skip", "stop"):
                    raise FlowError(f"{did}: on_not_applicable must be 'skip' or 'stop'")
                color = entry.get("color") or ""
                outcome = str(entry.get("outcome") or "").strip()
                if len(outcome) > 40:
                    raise FlowError(f"{did}: outcome is at most 40 characters")
                if color not in STEP_COLORS:
                    raise FlowError(f"{did}: color must be one of {', '.join(sorted(STEP_COLORS - {''}))}")
                on_failure = entry.get("on_failure") or "stop"
                if on_failure not in ("stop", "continue"):
                    raise FlowError(f"{did}: on_failure must be 'stop' or 'continue'")
            d = TaskDefinition.objects.filter(pk=did).first()
            if d is None:
                raise FlowError(f"unknown definition {did}")
            ok, why = eligible(d, allow_high_risk=playbook.allow_high_risk)
            if not ok:
                raise FlowError(f"{d.name}: {why}")
            err = validate_params_override(d.parsed_spec or {}, override)
            if err is not None:
                raise FlowError(f"{d.name}: {err}")
            err = validate_step_inputs(d, step_inputs_mapping, playbook.inputs)
            if err is not None:
                raise FlowError(f"{d.name}: {err}")
            tree.append({"definition": d, "id": step_id, "params_override": override,
                         "inputs": step_inputs_mapping,
                         "on_not_applicable": on_not_applicable,
                         "on_failure": on_failure, "color": color, "outcome": outcome})
        return tree

    try:
        steps, flow = build_flow(load(definition_ids))
    except FlowError as exc:
        return Response({"detail": str(exc)}, status=400)
    playbook.steps.all().delete()
    PlaybookStep.objects.bulk_create([
        PlaybookStep(playbook=playbook, definition=st["definition"], order=i,
                     params_override=st["params_override"], inputs=st["inputs"],
                     on_not_applicable=st["on_not_applicable"],
                     on_failure=st["on_failure"], color=st["color"], outcome=st["outcome"],
                     step_id=st["step_id"], branch=st["branch"])
        for i, st in enumerate(steps)
    ])
    playbook.flow = flow
    playbook.save(update_fields=["flow"])
    return None


#: One playbook input per step is enough for anyone; the task spec's own cap.
MAX_PLAYBOOK_INPUTS = 16


def _validate_and_set_inputs(playbook: Playbook, raw, auto_enroll=None, *,
                             save: bool = True) -> Response | None:
    """Validate and set ``playbook.inputs``. Returns an error Response or None.

    Runs **before** the steps are validated: a step's input mapping is checked
    against the declarations. *auto_enroll* is the flag as the save will leave
    it — a PATCH can turn it on in the same request that declares the inputs.
    """
    from apps.tasks.spec import SpecError, _validate_inputs

    try:
        declared = _validate_inputs(raw)
    except SpecError as exc:
        return Response({"detail": str(exc)}, status=400)
    enrols = playbook.auto_enroll if auto_enroll is None else bool(auto_enroll)
    # Only a text input can be left empty; the other types always carry a value.
    if enrols and any(i.get("type", "text") == "text" and i.get("required") and not i.get("default")
                      for i in declared):
        # The same rule as the YAML importer's: nothing is ever here to fill an
        # input in when a schedule runs the playbook by itself.
        return Response(
            {"detail": "auto-enrolment runs without anyone to fill in inputs: "
                       "give every required input a default"},
            status=400)
    playbook.inputs = declared
    if save:
        playbook.save(update_fields=["inputs"])
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
        # Before the steps: a step's input mapping names these ids.
        if "inputs" in request.data and (
                err_inputs := _validate_and_set_inputs(playbook, request.data["inputs"])):
            transaction.set_rollback(True)
            return err_inputs
        err = _validate_and_set_steps(
            playbook, request.data.get("flow_steps") or request.data.get("definition_ids"))
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
    # Before the steps are validated: a step's input mapping names these ids.
    # The auto-enrol rule inside reads what this save will leave set, since
    # auto_enroll may be turning on in the same request.
    if "inputs" in data and (err_inputs := _validate_and_set_inputs(
            playbook, data["inputs"],
            auto_enroll=bool(data["auto_enroll"]) if "auto_enroll" in data
            else playbook.auto_enroll, save=False)):
        # Saved with the rest below, inside the transaction a bad step undoes.
        return err_inputs
    with transaction.atomic():
        playbook.save()
        if "flow_steps" in data or "definition_ids" in data:
            err = _validate_and_set_steps(
                playbook, data.get("flow_steps") or data.get("definition_ids"))
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


def _create_playbook_from_parsed(request, parsed, resolved, existing=None,
                                 *, name=None):
    """Create (or replace) a Playbook from parsed community YAML.

    *parsed* is :func:`community_yaml.parse`'s dict and *resolved* the same
    steps after :func:`community_yaml.resolve_steps`, so no caller re-implements
    the part that matters: the high-risk flag goes through the same two-factor
    gate a checkbox does, the inputs are declared before the steps that map to
    them, and the steps run past eligibility. Returns a Response — the row on
    success, the error with the transaction rolled back.

    *existing* updates a playbook in place (how Fork → edit → re-import works
    without accumulating duplicates); *name* overrides the parsed one, which is
    how the template library picks a free name when the example's own is taken.
    """
    playbook_id = request.data.get("playbook_id") if existing is None else None
    if existing is None and playbook_id:
        existing = get_object_or_404(
            scoping.filter_by_site(Playbook.objects.all(), request.user,
                                   cascade_global=True),
            pk=playbook_id)

    wanted = name or parsed["name"]
    clash = Playbook.objects.filter(name__iexact=wanted)
    if existing is not None:
        clash = clash.exclude(pk=existing.pk)
    if clash.exists():
        return Response({"detail": f"a playbook named {wanted!r} already exists"},
                        status=400)

    # A plain playbook runs in its `order`; a branching one in document order.
    steps = sorted(resolved, key=lambda s: s["order"])

    with transaction.atomic():
        playbook = existing or Playbook(created_by=request.user)
        playbook.name = wanted
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
        if err_inputs := _validate_and_set_inputs(playbook, parsed["inputs"]):
            transaction.set_rollback(True)
            return err_inputs
        playbook.save()
        def entry(s):
            return {"definition_id": str(s["definition"].id), "id": s.get("id"),
                    "params_override": s["params_override"],
                    "inputs": s.get("inputs") or {},
                    "on_not_applicable": s.get("on_not_applicable") or "stop",
                    "on_failure": s.get("on_failure") or "stop",
                    "color": s.get("color") or "",
                    "outcome": s.get("outcome") or ""}

        if parsed.get("tree"):
            by_index = {i: s for i, s in enumerate(resolved)}

            def rebuild(nodes):
                return [entry(by_index[n["step"]]) if "step" in n
                        else {"if": n["if"], "then": rebuild(n["then"]),
                              "else": rebuild(n["else"])}
                        for n in nodes]
            items = rebuild(parsed["tree"])
        else:
            items = [entry(s) for s in steps]
        err = _validate_and_set_steps(playbook, items)
        if err is not None:
            transaction.set_rollback(True)
            return err

    return Response(_row(playbook),
                    status=status.HTTP_200_OK if existing
                    else status.HTTP_201_CREATED)


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
        resolved = resolve_steps(parsed["steps"], visible_defs,
                                 community_names_by_slug("tasks"))
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    return _create_playbook_from_parsed(request, parsed, resolved)


def _library_dir() -> Path:
    return Path(__file__).resolve().parent / "library"


def _library_texts() -> dict[str, str]:
    return {p.stem: p.read_text() for p in _library_dir().glob("*.yaml")}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def playbook_library(request):
    """The template playbooks bundled with the server, one file per example.

    Parsed rather than listed blind: a template that no longer resolves should
    be missing from the picker rather than offered and refused, so a step whose
    task was renamed drops the template out of the list.
    """
    from vigil.contentyaml import ContentYamlError

    from .community_yaml import parse, resolve_steps

    builtins = TaskDefinition.objects.filter(owner=None,
                                             archived_at__isnull=True)
    results = []
    for template_id, text in sorted(_library_texts().items()):
        try:
            parsed = parse(text)
            resolved = resolve_steps(parsed["steps"], builtins)
        except ContentYamlError:
            continue
        results.append({
            "id": template_id,
            "name": parsed["name"],
            "description": parsed["description"],
            "steps": [s["definition"].name for s in
                      sorted(resolved, key=lambda s: s["order"])],
            "allow_high_risk": parsed["allow_high_risk"],
            "inputs": parsed["inputs"],
        })
    return Response({"results": results})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def playbook_library_use(request, template_id):
    """Create a playbook from one of the bundled templates.

    The steps resolve against the built-in tasks the server seeds, so the
    template works on a fresh install with nothing forked first. The example's
    own name is used when it is free and ``name (2)``, ``name (3)`` … when it
    is not, because these are starting points people keep several of.
    """
    from vigil.contentyaml import ContentYamlError

    from .community_yaml import parse, resolve_steps

    text = _library_texts().get(template_id)
    if text is None:
        return Response({"detail": "no such template"}, status=404)
    try:
        parsed = parse(text)
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    builtins = TaskDefinition.objects.filter(owner=None,
                                            archived_at__isnull=True)
    try:
        resolved = resolve_steps(parsed["steps"], builtins)
    except ContentYamlError as exc:
        return Response({"detail": str(exc)}, status=400)

    base = parsed["name"]
    taken = {n.lower() for n in Playbook.objects.values_list("name", flat=True)}
    wanted = base
    n = 1
    while wanted.lower() in taken:
        n += 1
        if n > 100:
            return Response({"detail": f"a playbook named {base!r} already exists"},
                            status=400)
        wanted = f"{base} ({n})"
    return _create_playbook_from_parsed(request, parsed, resolved, name=wanted)


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
