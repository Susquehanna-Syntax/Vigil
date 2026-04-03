from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.hosts.authentication import authenticate_agent

from .models import Task
from .serializers import TaskSerializer

_TERMINAL_STATES = {Task.State.COMPLETED, Task.State.FAILED, Task.State.REJECTED}
_UPDATABLE_STATES = {Task.State.DISPATCHED, Task.State.EXECUTING}


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

    task.state = new_state
    task.result_output = output
    task.completed_at = now()
    task.save()

    return Response(TaskSerializer(task).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_list(request):
    qs = Task.objects.select_related("host", "requested_by").order_by("-created_at")
    if host_id := request.query_params.get("host"):
        qs = qs.filter(host_id=host_id)
    if state := request.query_params.get("state"):
        qs = qs.filter(state=state)
    return Response(TaskSerializer(qs[:50], many=True).data)