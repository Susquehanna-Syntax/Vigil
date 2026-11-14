"""apps.tasks.views: definitions views (split from the old single module, unchanged)."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403
from ..api_schema import (
    TasksArchiveSerializer,
    TasksDefinitionErrorSerializer,
    TasksDefinitionListRowSerializer,
    TasksDefinitionSerializer,
    TasksEditorTemplatesSerializer,
    TasksNotFoundSerializer,
    TasksRefusalsSerializer,
    TasksTaskSerializer,
    TasksValidateRequestSerializer,
    TasksValidateResponseSerializer,
)


@extend_schema(responses=TasksEditorTemplatesSerializer)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def editor_templates(request):
    """The bundled task templates, in index order, each with its YAML."""
    return Response({"results": _load_editor_templates()})


@extend_schema(methods=["GET"],
               parameters=[
                   OpenApiParameter("scope", str, enum=["mine", "community"],
                                    description="Whose definitions; mine by default."),
                   OpenApiParameter("archived", str, enum=["1"],
                                    description="1 lists the archived ones instead."),
               ],
               responses=TasksDefinitionListRowSerializer(many=True))
@extend_schema(methods=["POST"], request=TasksValidateRequestSerializer,
               responses={201: TasksDefinitionSerializer,
                          400: TasksDefinitionErrorSerializer,
                          403: TasksDefinitionErrorSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def definition_list(request):
    """List a user's definitions, or create a new one from YAML."""
    if request.method == "GET":
        scope = request.query_params.get("scope", "mine")
        if scope == "community":
            qs = TaskDefinition.objects.filter(
                visibility=TaskDefinition.Visibility.COMMUNITY
            )
        else:
            qs = TaskDefinition.objects.filter(owner=request.user)
        if request.query_params.get("archived") == "1":
            qs = qs.filter(archived_at__isnull=False)
        else:
            qs = qs.filter(archived_at__isnull=True)
        qs = qs.select_related("owner").order_by("-updated_at")
        data = TaskDefinitionSerializer(qs, many=True).data
        if scope != "community":
            # Each task's most recent run, summarised for the library card's
            # donut (M6 08c). One run per task, newest first.
            from ..summary import run_summary
            newest_ids: dict = {}
            for run_id, def_id in (TaskRun.objects.filter(definition__in=[d["id"] for d in data])
                                   .order_by("-created_at").values_list("id", "definition_id")):
                newest_ids.setdefault(str(def_id), run_id)
            latest = {str(r.definition_id): r for r in TaskRun.objects.filter(
                id__in=newest_ids.values()).select_related("definition").prefetch_related("tasks")}
            for row in data:
                run = latest.get(str(row["id"]))
                row["last_run"] = ({"id": str(run.id), "created_at": run.created_at.isoformat(),
                                    "state": run.state, **{k: v for k, v in run_summary(run).items()
                                                           if k != "per_host"}}
                                   if run else None)
        return Response(data)

    from ..authz import author_denied
    denied = author_denied(request.user)
    if denied:
        return denied
    yaml_source = request.data.get("yaml_source", "")
    # All tasks are created private. Sharing happens through the explicit
    # publish endpoint, which later will gate on community-repo upload.
    definition = TaskDefinition(
        owner=request.user, visibility=TaskDefinition.Visibility.PRIVATE
    )
    try:
        _save_definition_from_yaml(definition, yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    definition.save()
    return Response(TaskDefinitionSerializer(definition).data, status=201)


@extend_schema(methods=["GET"], responses={200: TasksDefinitionSerializer,
                                           404: TasksNotFoundSerializer})
@extend_schema(methods=["PUT"], request=TasksValidateRequestSerializer,
               responses={200: TasksDefinitionSerializer,
                          400: TasksDefinitionErrorSerializer,
                          403: TasksDefinitionErrorSerializer,
                          404: TasksNotFoundSerializer})
@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def definition_detail(request, definition_id):
    """Fetch or update a definition. Definitions can't be deleted — playbooks
    and automations reference them, and a vanished definition would silently
    gut those sequences."""
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)

    if request.method == "GET":
        return Response(TaskDefinitionSerializer(definition).data)

    # Mutations require ownership, and a role that may write tasks at all.
    if definition.owner_id != request.user.id:
        return Response({"error": "You do not own this definition"}, status=403)
    from ..authz import author_denied
    denied = author_denied(request.user)
    if denied:
        return denied

    # PUT — update from new YAML source. Visibility is NOT editable here;
    # use the publish/unpublish endpoints.
    yaml_source = request.data.get("yaml_source", "")
    try:
        _save_definition_from_yaml(definition, yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    definition.save()
    return Response(TaskDefinitionSerializer(definition).data)


@extend_schema(request=TasksValidateRequestSerializer,
               responses={200: TasksValidateResponseSerializer,
                          400: TasksDefinitionErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_validate(request):
    """Validate YAML without persisting — used by the editor's live preview."""
    yaml_source = request.data.get("yaml_source", "")
    try:
        spec = parse_and_validate(yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)
    return Response({"ok": True, "parsed_spec": spec})


@extend_schema(request=None, responses={201: TasksDefinitionSerializer,
                                        403: TasksDefinitionErrorSerializer,
                                        404: TasksNotFoundSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_fork(request, definition_id):
    """Fork a community template into the current user's library."""
    from ..authz import author_denied
    denied = author_denied(request.user)
    if denied:
        return denied
    source = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(source, request.user):
        return Response({"error": "Not found"}, status=404)

    copy = TaskDefinition(
        owner=request.user,
        name=source.name,
        description=source.description,
        relevance=source.relevance,
        risk_level=source.risk_level,
        visibility=TaskDefinition.Visibility.PRIVATE,
        yaml_source=source.yaml_source,
        parsed_spec=source.parsed_spec,
        forked_from=source,
    )
    copy.save()
    return Response(TaskDefinitionSerializer(copy).data, status=201)


@extend_schema(parameters=[OpenApiParameter(
                   "host_ids", str, description="Comma-separated host ids, at most 500.")],
               responses={200: TasksRefusalsSerializer, 404: TasksNotFoundSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def definition_refusals(request, definition_id):
    """Which of ``?host_ids=a,b`` would refuse this task, per their agents'
    reported allowlists — for the deploy dialog's warning (M7)."""
    import uuid as _uuid

    from vigil import scoping

    from ..refusals import refusals_for

    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)
    ids = []
    for raw in (request.query_params.get("host_ids") or "").split(","):
        try:
            ids.append(_uuid.UUID(raw.strip()))
        except ValueError:
            continue   # a malformed id matches no host
    hosts = scoping.filter_by_site(Host.objects.filter(id__in=ids[:500]), request.user)
    return Response({"refusals": refusals_for(hosts, definition.parsed_spec or {})})


@extend_schema(responses={200: TasksTaskSerializer, 404: TasksNotFoundSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_detail(request, task_id):
    """Single-task fetch. History rows are the audit trail — who ran what,
    where, with what result — and are immutable by design; there is no
    delete."""
    from vigil import scoping
    task = get_object_or_404(
        Task.objects.select_related("host", "run"),
        pk=task_id,
    )
    if not scoping.host_in_scope(request.user, task.host):
        return Response({"error": "Not found"}, status=404)
    return Response(TaskSerializer(task).data)


@extend_schema(responses=OpenApiTypes.OBJECT,
               description="Action name to its label, risk, required and optional "
               "params and outputs.")
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def action_registry(request):
    """Expose the action registry for the editor's autocomplete / validation."""
    return Response(ACTION_REGISTRY)


@extend_schema(request=TasksArchiveSerializer,
               responses={200: TasksDefinitionSerializer,
                          403: TasksDefinitionErrorSerializer,
                          404: TasksNotFoundSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_archive(request, definition_id):
    """Archive or restore a task definition.

    Definitions are never deleted — a playbook or automation still pointing at
    one would lose a step without saying so — and this is the retirement path
    instead. Everything already referencing it keeps running.
    """
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)
    # Seeing a community definition is not owning it: archiving retires it for
    # everyone, so only its owner (with a role that writes tasks) may.
    from apps.accounts.models import Role
    from apps.accounts.permissions import OWNER, role_of

    from ..authz import author_denied
    denied = author_denied(request.user)
    if denied:
        return denied
    if definition.owner_id != request.user.id and role_of(request.user) not in (OWNER, Role.ADMIN):
        return Response({"error": "You do not own this definition"}, status=403)

    restore = bool(request.data.get("restore"))
    definition.archived_at = None if restore else now()
    definition.save(update_fields=["archived_at"])
    return Response(TaskDefinitionSerializer(definition).data)
