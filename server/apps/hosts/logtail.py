"""Live container logs (M11): open a view, poll it, and let the agent feed it.

Opening a view is a signed task like any container action — admin only, with
a TOTP code — whose ``container_logs`` step returns the last lines and then
streams new ones to this session while the view keeps polling. Outbound
only: the agent posts lines; the server never connects to it.
"""

from __future__ import annotations

import re
import secrets

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil import scoping

from .authentication import authenticate_agent
from .models import LogTailSession

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _task_for(session: LogTailSession, tail: int, user):
    import yaml

    from apps.tasks.dispatch import task_params
    from apps.tasks.models import Task
    from apps.tasks.spec import parse_and_validate

    spec = parse_and_validate(yaml.safe_dump({
        "name": f"Logs: {session.container_name}", "risk": "low",
        "actions": [{"id": "logs", "type": "container_logs",
                     "params": {"container_name": session.container_name, "tail": tail,
                                "session": str(session.id)}}]}))
    params, risk, expires_at = task_params(spec)
    return Task.objects.create(
        host=session.host, requested_by=user, step_label=f"logs: {session.container_name}",
        action="_script", params=params, risk_level=risk, state=Task.State.PENDING,
        expires_at=expires_at, nonce=secrets.token_hex(32))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def open_logs(request, host_id, name):
    from apps.accounts.totp import require_totp_confirmation

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    if not _NAME.match(name):
        return Response({"detail": "not a container name"}, status=status.HTTP_400_BAD_REQUEST)
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"Reading logs needs confirmation: {error}", "needs_totp": True},
                        status=status.HTTP_401_UNAUTHORIZED)
    try:
        tail = max(1, min(int(request.data.get("tail", 200)), 2000))
    except (TypeError, ValueError):
        tail = 200
    with transaction.atomic():
        session = LogTailSession.objects.create(host=host, container_name=name,
                                                requested_by=request.user)
        session.task = _task_for(session, tail, request.user)
        session.save(update_fields=["task"])
    return Response({"session": str(session.id), "task": str(session.task_id)},
                    status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def poll_logs(request, session_id):
    session = get_object_or_404(LogTailSession.objects.select_related("task", "host"), pk=session_id)
    if not scoping.host_in_scope(request.user, session.host):
        return Response(status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        LogTailSession.objects.filter(pk=session.pk).update(closed=True)
        return Response(status=status.HTTP_204_NO_CONTENT)
    try:
        after = int(request.query_params.get("after", 0))
    except ValueError:
        after = 0
    LogTailSession.objects.filter(pk=session.pk).update(viewer_seen_at=now())
    task = session.task
    return Response({
        "task_state": task.state if task else "gone",
        "initial": (task.result_output if task and task.state in ("completed", "failed") else ""),
        "lines": [row for row in session.lines if row[0] > after],
        "live": session.alive(now()),
    })


@api_view(["POST"])
@permission_classes([AllowAny])
def agent_log_lines(request, session_id):
    """The agent's side: append lines; answer whether to keep going."""
    host, err = authenticate_agent(request)
    if err:
        return err
    with transaction.atomic():
        session = (LogTailSession.objects.select_for_update()
                   .filter(pk=session_id, host=host).first())
        if session is None:
            return Response({"continue": False}, status=status.HTTP_404_NOT_FOUND)
        raw = request.data.get("lines")
        lines = [str(x)[:4000] for x in raw[:500]] if isinstance(raw, list) else []
        rows = list(session.lines or [])
        for line in lines:
            rows.append([session.next_seq, line])
            session.next_seq += 1
        session.lines = rows[-LogTailSession.MAX_LINES:]
        session.save(update_fields=["lines", "next_seq"])
    return Response({"continue": session.alive(now())})
