"""apps.tasks.views: agent views (split from the old single module, unchanged)."""

from drf_spectacular.utils import extend_schema

from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403
from ..api_schema import (
    TasksErrorSerializer,
    TasksTaskResultRequestSerializer,
    TasksTaskSerializer,
)


@extend_schema(
    request=TasksTaskResultRequestSerializer,
    responses={
        200: TasksTaskSerializer,
        400: TasksErrorSerializer,
        401: TasksErrorSerializer,
        403: TasksErrorSerializer,
        404: TasksErrorSerializer,
    },
)
@scoping.system_view(
    reason="agent API: the caller is a host, authenticated by its agent token"
)
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
        task.result_data = _clean_step_results(request.data.get("steps"))
        task.completed_at = now()
        task.save()
        _ingest_hunt_matches(task, request.data.get("steps"))
        if new_state in (
            Task.State.COMPLETED,
            Task.State.FAILED,
            Task.State.NOT_APPLICABLE,
        ):
            from apps.vulns.detection import record_detection

            record_detection(task)

        if task.run_id:
            _advance_run_sequence(task)

        # If the parent definition is flagged ``collect:``, capture this
        # successful run's output into the host's inventory custom columns.
        if new_state == Task.State.COMPLETED:
            from apps.hosts.container_history import record_container_task

            record_container_task(task)
            _maybe_capture_inventory_column(task, output)
            _maybe_apply_tags(task)
            _maybe_request_nessus_scan(task)
            _maybe_ingest_trivy_report(task, output)
            _maybe_ingest_firewall_rules(task, output)
            _maybe_ingest_docker_usage(task, output)
        if new_state in (
            Task.State.COMPLETED,
            Task.State.FAILED,
            Task.State.NOT_APPLICABLE,
        ):
            # A step that ran before a later one failed still fired, so these
            # run on any terminal state and pick their steps from the agent's
            # per-step statuses rather than the task's.
            _maybe_run_alert_actions(task)
        if new_state in (Task.State.COMPLETED, Task.State.NOT_APPLICABLE):
            # A chain that stops as not applicable is finished on this host
            # too, so the completion tag must land here as well — otherwise
            # every reconcile pass redispatches the playbook.
            _maybe_apply_playbook_completion_tag(task)

    # Every state accepted here is terminal. After the block commits, so a
    # listener never sees a result that then rolls back. task_finished, not
    # task_completed: automations subscribe to the latter and have never been
    # sent it, so emitting it here would start firing them, with nothing to
    # stop a task-runs-a-task loop.
    from vigil import hooks

    hooks.emit("task_finished", task=task, host=host)

    return Response(TaskSerializer(task).data)
