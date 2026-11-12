"""apps.tasks.views: runs views (split from the old single module, unchanged)."""
from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("tasks")
def task_history(request):
    """Paginated task history feed for the History tab (polled by the UI).

    Returns the full fleet's task history, newest first, in pages of 50.
    """
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    page_size = 50

    from ..authz import visible_tasks
    qs = visible_tasks(
        Task.objects.filter(hidden=False)
        .select_related("host", "requested_by")
        .order_by("-created_at"),
        request.user,
    )
    total = qs.count()
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    start = (page - 1) * page_size
    items = list(qs[start:start + page_size])
    return Response({
        "count": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "results": TaskSerializer(items, many=True).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@scoping.module_view("tasks")
def run_history(request):
    """Paginated run history, newest first.

    ``?source=automation,playbook`` narrows to those kinds; omitted means
    every run including manual deploys.
    """
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    page_size = 25

    from ..authz import trim_runs, visible_runs
    qs = visible_runs(TaskRun.objects.select_related(
        "automation", "playbook", "requested_by").order_by("-created_at"), request.user)

    raw = (request.query_params.get("source") or "").strip()
    if raw:
        wanted = [s for s in (v.strip() for v in raw.split(",")) if s]
        valid = [s for s in wanted if s in TaskRun.Source.values]
        if not valid:
            return Response({"detail": f"unknown source {raw!r}"}, status=400)
        qs = qs.filter(source__in=valid)

    total = qs.count()
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    start = (page - 1) * page_size
    return Response({
        "count": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "results": TaskRunSummarySerializer(
            trim_runs(qs[start:start + page_size], request.user), many=True).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id):
    run = get_object_or_404(
        TaskRun.objects.prefetch_related("tasks__host").select_related(
            "definition", "requested_by"
        ),
        pk=run_id,
    )
    from ..authz import run_visible, trim_run
    if not run_visible(request.user, run):
        return Response({"error": "Not found"}, status=404)
    data = TaskRunSerializer(trim_run(run, request.user)).data
    from ..summary import run_summary
    data["summary"] = run_summary(run)
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_hunt_results(request, run_id):
    """The spec's results view: one call returns every hunt match of a run.

    ``columns`` is the union of the matches' own field names, first-seen
    order; ``hosts`` gives every targeted host its state (matched / not
    matched / pending / error), match count and whether any of its hunts
    was truncated or timed out; ``matches`` is one page of the matches.
    ``?host=`` and ``?step=`` filter the matches only — hosts and columns
    always describe the whole run. A run without a hunt step is a 404.
    """
    run = get_object_or_404(
        TaskRun.objects.prefetch_related("tasks__host"), pk=run_id)
    from ..authz import run_visible
    if not run_visible(request.user, run):
        return Response({"error": "Not found"}, status=404)
    tasks = list(run.tasks.all())
    # Read the signed steps each task carried, so playbook and rollout
    # runs (no single definition) are recognised the same way. A task's
    # relevant: probes are hunts too, and their matches are its evidence.
    if not any(
        isinstance((task.params or {}).get("relevant"), dict)
        or any(
            str(step.get("action", "")).startswith("hunt_")
            for step in ((task.params or {}).get("steps") or [])
            if isinstance(step, dict)
        )
        for task in tasks
    ):
        raise Http404("run has no hunt steps")

    try:
        limit = max(0, min(5000, int(request.query_params.get("limit", 1000))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        return Response({"detail": "limit and offset must be integers"}, status=400)

    run_matches = HuntMatch.objects.filter(run=run)
    columns: dict = {}
    for data in run_matches.values_list("data", flat=True).iterator(chunk_size=2000):
        for key in (data or {}):
            columns.setdefault(key, None)

    matches_qs = run_matches.select_related("host").order_by(
        "host__hostname", "step_id", "id")
    if raw_host := (request.query_params.get("host") or "").strip():
        try:
            matches_qs = matches_qs.filter(host_id=uuid.UUID(raw_host))
        except ValueError:
            return Response({"detail": "host must be a host id"}, status=400)
    if raw_step := (request.query_params.get("step") or "").strip():
        matches_qs = matches_qs.filter(step_id=raw_step)

    total = matches_qs.count()
    matches = []
    for row in matches_qs[offset:offset + limit]:
        item = {
            "host_id": str(row.host_id),
            "hostname": row.host.hostname,
            "step_id": row.step_id,
            "action": row.action,
            "evidence_type": row.evidence_type,
        }
        # Agent-reported fields never overwrite the server's own keys.
        item.update({k: v for k, v in (row.data or {}).items() if k not in item})
        matches.append(item)

    counts = dict(run_matches.order_by().values_list("task_id").annotate(n=Count("id")))
    hosts = []
    for task in sorted(tasks, key=lambda t: (t.host.hostname, str(t.id))):
        count = counts.get(task.id, 0)
        step_hunts = (task.result_data or {}).get("hunts") or {}
        hosts.append({
            "host_id": str(task.host_id),
            "hostname": task.host.hostname,
            "state": _hunt_host_state(task, count),
            "match_count": count,
            "truncated": any(bool(h.get("truncated")) for h in step_hunts.values()
                             if isinstance(h, dict)),
            "timed_out": any(bool(h.get("timed_out")) for h in step_hunts.values()
                             if isinstance(h, dict)),
        })

    return Response({
        "columns": list(columns),
        "hosts": hosts,
        "matches": matches,
        "total": total,
        "limit": limit,
        "offset": offset,
    })
