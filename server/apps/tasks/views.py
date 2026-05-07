import secrets
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.hosts.authentication import authenticate_agent
from apps.hosts.models import Host

from .models import Task, TaskDefinition, TaskRun
from .serializers import (
    TaskDefinitionSerializer,
    TaskRunSerializer,
    TaskSerializer,
)
from .spec import (
    ACTION_REGISTRY,
    SpecError,
    _validate_on_failure,
    _validate_schedule,
    _validate_success_criteria,
    parse_and_validate,
    resolve_inputs,
)

_TERMINAL_STATES = {Task.State.COMPLETED, Task.State.FAILED, Task.State.REJECTED}
_UPDATABLE_STATES = {Task.State.DISPATCHED, Task.State.EXECUTING}

# Legacy single-action dispatch map (kept for the existing one-off dispatch UI).
_VALID_ACTIONS = {name: info["required"] for name, info in ACTION_REGISTRY.items()}
_ACTION_RISK = {
    name: getattr(Task.RiskLevel, info["risk"].upper())
    for name, info in ACTION_REGISTRY.items()
}


# ── Agent-facing: task result ────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def task_result(request):
    host, err = authenticate_agent(request)
    if err:
        return err

    task_id = request.data.get("task_id")
    new_state = request.data.get("state")
    output = request.data.get("output", "")

    if not task_id or not new_state:
        return Response({"error": "task_id and state are required"}, status=400)

    if new_state not in _TERMINAL_STATES:
        return Response(
            {"error": f"state must be one of: {', '.join(sorted(_TERMINAL_STATES))}"},
            status=400,
        )

    try:
        task = Task.objects.get(pk=task_id, host=host)
    except Task.DoesNotExist:
        return Response({"error": "Task not found"}, status=404)

    if task.state not in _UPDATABLE_STATES:
        return Response(
            {"error": f"Task is in state '{task.state}' and cannot be updated"},
            status=400,
        )

    with transaction.atomic():
        task.state = new_state
        task.result_output = output
        task.completed_at = now()
        task.save()

        if task.run_id:
            _advance_run_sequence(task)

        # If the parent definition is flagged ``collect:``, capture this
        # successful run's output into the host's inventory custom columns.
        if new_state == Task.State.COMPLETED:
            _maybe_capture_inventory_column(task, output)

    return Response(TaskSerializer(task).data)


def _maybe_capture_inventory_column(task: Task, output: str) -> None:
    """Write task output into ``HostInventory.custom_columns`` if applicable.

    Skips silently when the task isn't part of a run, the run has no
    definition, or the definition's parsed_spec lacks a ``collect`` block.
    """
    from apps.hosts.models import HostInventory

    run = task.run
    definition = getattr(run, "definition", None) if run else None
    if not definition:
        return
    collect = (definition.parsed_spec or {}).get("collect") or None
    if not collect or not isinstance(collect, dict):
        return
    column = (collect.get("column") or "").strip()
    if not column:
        return
    parse_mode = collect.get("parse") or "output_line_1"

    text = output or ""
    if parse_mode == "output_line_1":
        # Strip the per-step bracketed prefix our agent reports use, then
        # take the first non-empty line of the task output.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        if first.startswith("[OK]"):
            first = first[4:].lstrip()
            if ":" in first:
                first = first.split(":", 1)[1].strip()
        value = first[:500]
    elif parse_mode == "output_trim":
        value = text.strip()[:500]
    else:  # output_full
        value = text[:2000]

    inv, _ = HostInventory.objects.get_or_create(host=task.host)
    columns = dict(inv.custom_columns or {})
    columns[column] = value
    inv.custom_columns = columns
    inv.save(update_fields=["custom_columns", "updated_at"])


def _advance_run_sequence(finished_task: Task) -> None:
    """After a task in a run finishes, unblock the next step on that host.

    If the finished task failed or was rejected, retry policy is consulted:
    when ``retry_count < max_retries`` the original task is reset to PENDING
    (with a fresh nonce/signature and a ``not_before`` delay) so the agent
    re-executes it on the next eligible checkin. Once retries are exhausted
    or no policy applies, all remaining blocked steps on that host are
    rejected. Finally, the run is collapsed into a terminal state when no
    tasks remain eligible to run.
    """
    run = finished_task.run
    sibling_qs = Task.objects.filter(
        run=run, host=finished_task.host, step_order__gt=finished_task.step_order
    ).order_by("step_order")

    if finished_task.state == Task.State.COMPLETED:
        next_step = sibling_qs.filter(state=Task.State.BLOCKED).first()
        if next_step:
            next_step.state = Task.State.PENDING
            next_step.save(update_fields=["state"])
    else:
        # Failure path — try to retry the same step before aborting the chain.
        if (
            finished_task.state == Task.State.FAILED
            and finished_task.retry_count < finished_task.max_retries
        ):
            delay = max(0, int(finished_task.retry_delay_seconds or 0))
            finished_task.retry_count += 1
            finished_task.state = Task.State.PENDING
            finished_task.nonce = secrets.token_hex(32)
            finished_task.signature = ""
            finished_task.dispatched_at = None
            finished_task.completed_at = None
            finished_task.not_before = now() + timedelta(seconds=delay) if delay else None
            prior = (finished_task.result_output or "").rstrip()
            attempt_marker = (
                f"\n[retry {finished_task.retry_count}/{finished_task.max_retries} "
                f"scheduled, waiting {delay}s]"
            )
            finished_task.result_output = (prior + attempt_marker).strip()
            finished_task.save(update_fields=[
                "retry_count", "state", "nonce", "signature",
                "dispatched_at", "completed_at", "not_before", "result_output",
            ])
            # Don't finalize the run — there's still active work pending.
            return

        # No retry remaining — abort the rest of the chain for this host.
        sibling_qs.filter(state=Task.State.BLOCKED).update(
            state=Task.State.REJECTED,
            result_output=f"Aborted: step {finished_task.step_order} did not succeed",
            completed_at=now(),
        )

    _finalize_run_if_done(run)


def _finalize_run_if_done(run: TaskRun) -> None:
    active_states = {
        Task.State.BLOCKED,
        Task.State.PENDING,
        Task.State.DISPATCHED,
        Task.State.EXECUTING,
    }
    if Task.objects.filter(run=run, state__in=active_states).exists():
        return

    states = set(Task.objects.filter(run=run).values_list("state", flat=True))
    if states <= {Task.State.COMPLETED}:
        run.state = TaskRun.State.COMPLETED
    elif Task.State.COMPLETED in states:
        run.state = TaskRun.State.PARTIAL
    else:
        run.state = TaskRun.State.FAILED
    run.finished_at = now()
    run.save(update_fields=["state", "finished_at"])


# ── Legacy single-action dispatch (kept for the quick-action UI) ─────────────


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def task_list(request):
    if request.method == "GET":
        qs = Task.objects.select_related("host", "requested_by").order_by("-created_at")
        if host_id := request.query_params.get("host"):
            qs = qs.filter(host_id=host_id)
        if state := request.query_params.get("state"):
            qs = qs.filter(state=state)
        return Response(TaskSerializer(qs[:50], many=True).data)

    action = request.data.get("action", "").strip()
    host_id = request.data.get("host", "")
    params = request.data.get("params") or {}

    if not action:
        return Response({"error": "action is required"}, status=400)
    if action not in _VALID_ACTIONS:
        return Response({"error": f"Unknown action '{action}'"}, status=400)
    if not host_id:
        return Response({"error": "host is required"}, status=400)

    for required_param in _VALID_ACTIONS[action]:
        if required_param not in params:
            return Response({"error": f"Missing required param: {required_param}"}, status=400)

    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return Response({"error": "Host not found"}, status=404)

    if host.status != Host.Status.ONLINE:
        return Response({"error": "Host is not online"}, status=400)
    if host.mode == Host.Mode.MONITOR:
        return Response({"error": "Host is in monitor mode — task execution disabled"}, status=400)

    task = Task.objects.create(
        host=host,
        requested_by=request.user,
        action=action,
        params=params,
        risk_level=_ACTION_RISK.get(action, Task.RiskLevel.STANDARD),
        nonce=secrets.token_hex(32),
    )
    return Response(TaskSerializer(task).data, status=201)


# ── TaskDefinition CRUD ──────────────────────────────────────────────────────


def _user_can_see(definition: TaskDefinition, user) -> bool:
    return (
        definition.visibility == TaskDefinition.Visibility.COMMUNITY
        or definition.owner_id == user.id
    )


def _save_definition_from_yaml(definition: TaskDefinition, yaml_source: str) -> None:
    spec = parse_and_validate(yaml_source)
    definition.yaml_source = yaml_source
    definition.parsed_spec = spec
    definition.name = spec["name"]
    definition.description = spec["description"]
    definition.relevance = spec["relevance"]
    definition.risk_level = spec["risk"]


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
        qs = qs.select_related("owner").order_by("-updated_at")
        return Response(TaskDefinitionSerializer(qs, many=True).data)

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


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def definition_detail(request, definition_id):
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)

    if request.method == "GET":
        return Response(TaskDefinitionSerializer(definition).data)

    # Mutations require ownership.
    if definition.owner_id != request.user.id:
        return Response({"error": "You do not own this definition"}, status=403)

    if request.method == "DELETE":
        definition.delete()
        return Response(status=204)

    # PUT — update from new YAML source. Visibility is NOT editable here;
    # use the publish/unpublish endpoints.
    yaml_source = request.data.get("yaml_source", "")
    try:
        _save_definition_from_yaml(definition, yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    definition.save()
    return Response(TaskDefinitionSerializer(definition).data)


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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_publish(request, definition_id):
    """Publish a definition to the community tab.

    Only the owner can publish. Later, this endpoint will also push the
    definition to an external community repo (a separate site that hosts a
    self-hostable task library) — for now it just flips the local visibility.
    """
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if definition.owner_id != request.user.id:
        return Response({"error": "You do not own this definition"}, status=403)
    definition.visibility = TaskDefinition.Visibility.COMMUNITY
    definition.save(update_fields=["visibility", "updated_at"])
    return Response(TaskDefinitionSerializer(definition).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_unpublish(request, definition_id):
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if definition.owner_id != request.user.id:
        return Response({"error": "You do not own this definition"}, status=403)
    definition.visibility = TaskDefinition.Visibility.PRIVATE
    definition.save(update_fields=["visibility", "updated_at"])
    return Response(TaskDefinitionSerializer(definition).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_fork(request, definition_id):
    """Fork a community template into the current user's library."""
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


# ── Deploy ───────────────────────────────────────────────────────────────────


def _verify_confirmation(user, payload) -> str | None:
    """Return error message if confirmation fails, else None.

    TOTP (RFC 6238) is required for task deploys. Users must enroll via
    Settings before they can deploy scripts to agents.
    """
    from apps.accounts.totp import verify_totp

    profile = getattr(user, "profile", None)
    totp_secret = getattr(profile, "totp_secret", "") or ""
    totp_enabled = bool(profile and profile.totp_confirmed_at and totp_secret)

    # Skip the gate entirely in DEBUG mode so local dev/testing works without TOTP.
    from django.conf import settings as _settings
    if _settings.DEBUG:
        return None

    if not totp_enabled:
        return "TOTP enrollment required — enroll in Settings before deploying"

    totp_code = (payload.get("totp") or "").strip()
    if not totp_code:
        return "TOTP code required"
    if not verify_totp(totp_secret, totp_code):
        return "Invalid TOTP code"
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_deploy(request, definition_id):
    """Deploy a definition across one or more hosts.

    Request body::
        {
          "host_ids": ["<uuid>", "<uuid>"],
          "password": "..."           # or "totp": "123456"
        }
    """
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)

    # Targeting: either an explicit list of host_ids or a list of tags. When
    # tags are supplied we resolve to the set of online, executable hosts
    # that match ANY of the requested tags (union semantics).
    raw_tags = request.data.get("tags") or []
    host_ids = request.data.get("host_ids") or []
    if raw_tags:
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            return Response({"error": "tags must be a list of strings"}, status=400)
        wanted = {t.strip().lower() for t in raw_tags if t.strip()}
        if not wanted:
            return Response({"error": "tags is empty after normalization"}, status=400)
        candidate_hosts = Host.objects.filter(
            status=Host.Status.ONLINE
        ).exclude(mode=Host.Mode.MONITOR)
        host_ids = [
            str(h.id)
            for h in candidate_hosts
            if any(isinstance(t, str) and t.lower() in wanted for t in (h.tags or []))
        ]
        if not host_ids:
            return Response(
                {"error": f"no eligible hosts match tags: {sorted(wanted)}"},
                status=400,
            )
    if not isinstance(host_ids, list) or not host_ids:
        return Response({"error": "host_ids must be a non-empty list"}, status=400)

    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=401)

    base_spec = definition.parsed_spec
    raw_inputs = request.data.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        return Response({"error": "inputs must be an object"}, status=400)
    try:
        spec = resolve_inputs(base_spec, raw_inputs)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    # Per-deploy policy overrides — Schedule / Retry / Success Criteria. The
    # deploy modal sends these from its policy tabs, hydrated from the YAML
    # defaults. Each override is validated using the same validators the YAML
    # parser uses, so an attacker can't smuggle a different schema through.
    try:
        if "schedule" in request.data:
            override = _validate_schedule(request.data.get("schedule"))
            spec["schedule"] = override
        if "on_failure" in request.data:
            override = _validate_on_failure(request.data.get("on_failure"))
            spec["on_failure"] = override
        if "success_criteria" in request.data:
            override = _validate_success_criteria(request.data.get("success_criteria"))
            spec["success_criteria"] = override
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    actions = spec.get("actions") or []
    if not actions:
        return Response({"error": "definition has no actions"}, status=400)

    hosts = list(Host.objects.filter(id__in=host_ids))
    if len(hosts) != len(host_ids):
        return Response({"error": "One or more hosts not found"}, status=404)

    for host in hosts:
        if host.status != Host.Status.ONLINE:
            return Response(
                {"error": f"Host {host.hostname} is not online"}, status=400
            )
        if host.mode == Host.Mode.MONITOR:
            return Response(
                {"error": f"Host {host.hostname} is in monitor mode"}, status=400
            )

    # Build the steps payload the agent will receive.  The full script is
    # sent as a single signed task per host — the agent validates each
    # action against its own local allowlist, so a compromised server
    # cannot escalate beyond what each agent permits.
    success_criteria = spec.get("success_criteria") or None
    steps_payload = []
    for i, action in enumerate(actions):
        step = {
            "id": action.get("id") or f"step{i + 1}",
            "action": action["type"],
            "params": action.get("params") or {},
        }
        # Success criteria apply to every step in the script. The agent
        # evaluates these after each step's exit and marks the step failed
        # if criteria are not met (even if the action itself succeeded).
        if success_criteria:
            step["success_criteria"] = success_criteria
        steps_payload.append(step)

    # Effective risk is the highest risk across all actions.
    risk = spec.get("risk", "standard")

    # Schedule + retry policy are snapshotted onto each Task so a later edit
    # of the TaskDefinition cannot retroactively change in-flight deploys.
    schedule_snapshot = spec.get("schedule") or {}
    retry_cfg = ((spec.get("on_failure") or {}).get("retry") or {})
    max_retries = int(retry_cfg.get("attempts", 0))
    retry_delay = int(retry_cfg.get("delay_seconds", 0))

    with transaction.atomic():
        run = TaskRun.objects.create(
            definition=definition,
            name_snapshot=definition.name,
            requested_by=request.user,
            host_count=len(hosts),
            step_count=len(actions),
            state=TaskRun.State.RUNNING,
        )

        for host in hosts:
            Task.objects.create(
                host=host,
                requested_by=request.user,
                run=run,
                step_order=0,
                step_label=definition.name,
                action="_script",
                params={"steps": steps_payload},
                risk_level=risk,
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
                schedule=schedule_snapshot,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay,
            )

    return Response(TaskRunSerializer(run).data, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id):
    run = get_object_or_404(
        TaskRun.objects.prefetch_related("tasks__host").select_related(
            "definition", "requested_by"
        ),
        pk=run_id,
    )
    return Response(TaskRunSerializer(run).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def action_registry(request):
    """Expose the action registry for the editor's autocomplete / validation."""
    return Response(ACTION_REGISTRY)
