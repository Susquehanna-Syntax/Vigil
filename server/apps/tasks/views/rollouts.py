"""apps.tasks.views: rollouts views (split from the old single module, unchanged)."""
from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def rollout_collection(request):
    """GET — list rollouts. POST — start one from a definition or a playbook.

    TOTP-gated like a manual deploy — it fans the work out across the whole
    fleet, wave by wave.
    """
    if request.method == "POST":
        from apps.playbooks.models import Playbook

        raw_tags = request.data.get("wave_group_tags")
        if raw_tags is not None and not (
            isinstance(raw_tags, list) and all(isinstance(t, str) for t in raw_tags)
        ):
            return Response({"detail": "wave_group_tags must be a list of strings"},
                            status=400)
        if raw_tags is not None and (request.data.get("wave_group_tag") or "").strip():
            return Response(
                {"detail": "send either wave_group_tag or wave_group_tags, not both"},
                status=400,
            )

        definition_id = request.data.get("definition_id")
        playbook_id = request.data.get("playbook_id")
        if bool(definition_id) == bool(playbook_id):
            return Response(
                {"detail": "supply exactly one of definition_id or playbook_id"},
                status=400,
            )
        from ..authz import fleet_runner_denied
        denied = fleet_runner_denied(request.user)
        if denied:
            return denied
        definition = playbook = None
        if definition_id:
            definition = get_object_or_404(TaskDefinition, pk=definition_id)
            if not _user_can_see(definition, request.user):
                return Response({"detail": "Not found"}, status=404)
        else:
            playbook = get_object_or_404(Playbook, pk=playbook_id)
        error = _verify_confirmation(request.user, request.data)
        if error:
            return Response({"detail": error}, status=401)

        inputs = request.data.get("inputs")
        if inputs is not None and not isinstance(inputs, dict):
            return Response({"detail": "inputs must be a mapping of input id to value"},
                            status=400)
        inputs = inputs or {}

        if raw_tags is not None:
            try:
                rollouts = start_rollout_batch(
                    definition,
                    playbook=playbook,
                    user=request.user,
                    group_tags=raw_tags,
                    failure_threshold_pct=int(request.data.get("failure_threshold_pct", 10)),
                    min_results_before_halt=int(request.data.get("min_results_before_halt", 3)),
                    inputs=inputs,
                )
            except (ValueError, TypeError) as exc:
                return Response({"detail": str(exc)}, status=400)
            return Response({
                "batch": str(rollouts[0].batch),
                "rollouts": [_rollout_response(r, request.user) for r in rollouts],
            }, status=201)

        try:
            rollout = start_rollout(
                definition,
                playbook=playbook,
                user=request.user,
                wave_group_tag=(request.data.get("wave_group_tag") or "").strip(),
                failure_threshold_pct=int(request.data.get("failure_threshold_pct", 10)),
                min_results_before_halt=int(request.data.get("min_results_before_halt", 3)),
                inputs=inputs,
            )
        except (ValueError, TypeError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_rollout_response(rollout, request.user), status=201)

    rollouts = PatchRollout.objects.select_related(
        "definition", "current_wave", "created_by", "halted_by", "resumed_by",
    ).order_by("-created_at")
    raw = (request.query_params.get("state") or "").strip()
    if raw:
        wanted = [s for s in (v.strip() for v in raw.split(",")) if s]
        valid = [s for s in wanted if s in PatchRollout.State.values]
        if not valid:
            return Response({"detail": f"unknown state {raw!r}"}, status=400)
        rollouts = rollouts.filter(state__in=valid)
    from ..authz import visible_rollouts
    return Response([_RolloutDetailSerializer(r, context={"user": request.user}).data
                     for r in visible_rollouts(rollouts, request.user)])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def rollout_detail(request, rollout_id):
    rollout = get_object_or_404(
        PatchRollout.objects.select_related(
            "definition", "current_wave", "created_by", "halted_by", "resumed_by",
        ),
        pk=rollout_id,
    )
    from ..authz import rollout_visible
    if not rollout_visible(request.user, rollout):
        return Response({"detail": "Not found"}, status=404)
    return Response(_RolloutDetailSerializer(rollout, context={"user": request.user}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def rollout_halt(request, rollout_id):
    """Stop the rollout now. Admin + TOTP: halting a fleet-wide patch
    mid-flight is the heavier of the two operator calls."""
    from ..authz import rollout_write_denied
    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    if denied := rollout_write_denied(request.user, rollout):
        return denied
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    reason = str(request.data.get("reason") or "").strip()
    try:
        halt_rollout(rollout, user=request.user, reason=reason)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(_rollout_response(rollout, request.user))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def rollout_batch_halt(request, batch):
    """Halt every running rollout in one batch. One request because one TOTP
    code is single-use — three rollouts would otherwise need three codes."""
    from ..authz import rollout_write_denied, visible_rollouts
    members = list(PatchRollout.objects.filter(batch=batch))
    if not visible_rollouts(members, request.user):
        return Response({"detail": "Not found"}, status=404)
    # One call halts the whole batch, so the caller must be allowed to act on
    # every rollout in it, including any they cannot see.
    for member in members:
        if rollout_write_denied(request.user, member):
            return Response({"detail": "This batch includes rollouts you may not halt"}, status=403)
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    reason = str(request.data.get("reason") or "").strip()
    try:
        halted = halt_batch(batch, user=request.user, reason=reason)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response({"halted": halted})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rollout_resume(request, rollout_id):
    """Clear a halt and continue from the same wave. TOTP-gated; records who
    did it. Failed tasks on the wave are re-queued so the gate re-evaluates
    over the whole wave."""
    from ..authz import rollout_write_denied
    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    if denied := rollout_write_denied(request.user, rollout):
        return denied
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    try:
        resume_rollout(rollout, user=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(_rollout_response(rollout, request.user))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wave_collection(request):
    """List waves in order, or create one."""
    from ..models import PatchWave
    from ..rollout_serializers import PatchWaveSerializer

    if request.method == "GET":
        waves = _visible_waves(request.user).prefetch_related("group_tag_rows")
        if group_tag := (request.query_params.get("group") or "").strip():
            waves = waves.filter(group_tag_rows__key=group_tag.lower())
        return Response(
            PatchWaveSerializer(waves.order_by("order", "id"), many=True).data)

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)
    # S1 04h: a site admin builds their own site's ladder; global waves are
    # an instance admin's.
    site_id, denied = scoping.site_for_write(request.user, request.data.get("site_id"))
    if denied:
        return denied
    serializer = PatchWaveSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)
    clash = _wave_order_conflict(
        serializer.validated_data.get("order"),
        serializer.validated_data.get("group_tags") or [], site_id=site_id)
    if clash is not None:
        where = (f"the {clash.group_tag_rows.first().name} group"
                 if clash.group_tag_rows.exists() else "the ungrouped waves")
        return Response(
            {"order": [f"Wave {serializer.validated_data.get('order')} "
                       f"already exists in {where} ({clash.name})."]},
            status=400,
        )
    serializer.save(site_id=site_id)
    return Response(serializer.data, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def wave_detail(request, wave_id):
    """Read, edit, or delete one wave."""
    from ..models import PatchRollout, PatchWave
    from ..rollout_serializers import PatchWaveSerializer

    wave = get_object_or_404(_visible_waves(request.user), pk=wave_id)

    if request.method == "GET":
        return Response(PatchWaveSerializer(wave).data)

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)
    denied = scoping.write_denied(request.user, wave)
    if denied:
        return denied

    if request.method == "DELETE":
        # Refuse while a rollout is standing on this wave. Deleting it would
        # null current_wave and strand the rollout with nothing to advance from.
        active = PatchRollout.objects.filter(
            current_wave=wave,
            state__in=[PatchRollout.State.RUNNING, PatchRollout.State.VALIDATING,
                       PatchRollout.State.HALTED],
        ).exists()
        if active:
            return Response(
                {"detail": "A rollout is currently on this wave. Halt or finish it first."},
                status=409,
            )
        wave.delete()
        return Response(status=204)

    serializer = PatchWaveSerializer(wave, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)
    try:
        serializer.save()
    except IntegrityError:
        return Response(
            {"order": [f"Wave {request.data.get('order')} already exists."]},
            status=400,
        )
    return Response(serializer.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def rollout_skip_validation(request, rollout_id):
    """End the current wave's validation window early and advance.

    Admin + TOTP, same as halting: it shortens the safety margin on a
    fleet-wide patch, which is an operator decision worth authenticating.
    """
    from ..authz import rollout_write_denied
    from ..rollout import skip_validation

    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    if denied := rollout_write_denied(request.user, rollout):
        return denied
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    try:
        skip_validation(rollout, user=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    rollout.refresh_from_db()
    return Response(_rollout_response(rollout, request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wave_group_collection(request):
    """The ladders that exist, derived from the tags waves carry.

    There is no wave-group record to keep in step with anything: a group is a
    tag, so this reports the distinct group tags in use and how many enabled
    waves each one holds. That is what the wave editor and the rollout picker
    need in order to offer them.
    """
    from ..models import PatchWave

    counts = {}
    waves = _visible_waves(request.user)
    for wave in waves.prefetch_related("group_tag_rows"):
        for tag in wave.group_tag_rows.all():
            entry = counts.setdefault(
                tag.key, {"tag": tag.name, "waves": 0, "enabled_waves": 0})
            entry["waves"] += 1
            if wave.enabled:
                entry["enabled_waves"] += 1

    ungrouped = waves.filter(group_tag_rows__isnull=True).count()
    return Response({
        "groups": sorted(counts.values(), key=lambda g: g["tag"].lower()),
        "ungrouped_waves": ungrouped,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def rollout_wave_hosts(request, rollout_id, wave_id):
    """Every machine in one wave of one rollout, and how it went.

    The wave summary only ever said how many failed. Finding out *which*
    machines, and why, meant going to the run history and matching up hosts by
    hand — so a halted rollout was hard to act on at the moment it mattered.
    """
    from ..authz import rollout_visible, visible_tasks
    from ..models import PatchRollout, Task

    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    if not rollout_visible(request.user, rollout):
        return Response({"detail": "Not found"}, status=404)
    tasks = visible_tasks(Task.objects
             .filter(run__rollout=rollout, run__wave_id=wave_id, step_order=0)
             .select_related("host", "run")
             .order_by("host__hostname"), request.user)

    rows = []
    for task in tasks:
        rows.append({
            "task_id": str(task.id),
            "host_id": str(task.host_id),
            "hostname": task.host.hostname,
            "ip_address": task.host.ip_address,
            "state": task.state,
            "failed": task.state in FAILURE_STATES,
            "dispatched_at": task.dispatched_at.isoformat() if task.dispatched_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "step_label": task.step_label,
            # The operator is here because something failed; the output is the
            # answer to "why", so it travels with the row rather than costing
            # another request per machine.
            "output": task.result_output or "",
        })

    return Response({
        "rollout_id": str(rollout.id),
        "wave_id": wave_id,
        "total": len(rows),
        "failed": sum(1 for r in rows if r["failed"]),
        "hosts": rows,
    })
