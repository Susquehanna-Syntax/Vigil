import secrets

from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.hosts.authentication import authenticate_agent
from apps.hosts.models import Host

from .models import Task
from .serializers import TaskSerializer

_TERMINAL_STATES = {Task.State.COMPLETED, Task.State.FAILED, Task.State.REJECTED}
_UPDATABLE_STATES = {Task.State.DISPATCHED, Task.State.EXECUTING}

# Actions known to the agent executor and their required params.
_VALID_ACTIONS = {
    "restart_service":   ["service_name"],
    "restart_container": ["container_name"],
    "stop_container":    ["container_name"],
    "start_container":   ["container_name"],
    "clear_temp_files":  [],
    "clear_docker_logs": [],
    "run_package_updates": [],
    "execute_script":    ["script_content"],
    "reboot":            [],
}

_ACTION_RISK = {
    "start_container":    Task.RiskLevel.LOW,
    "clear_temp_files":   Task.RiskLevel.LOW,
    "clear_docker_logs":  Task.RiskLevel.LOW,
    "restart_service":    Task.RiskLevel.STANDARD,
    "restart_container":  Task.RiskLevel.STANDARD,
    "stop_container":     Task.RiskLevel.STANDARD,
    "run_package_updates": Task.RiskLevel.STANDARD,
    "execute_script":     Task.RiskLevel.HIGH,
    "reboot":             Task.RiskLevel.HIGH,
}


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

    # POST — dispatch a new task
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