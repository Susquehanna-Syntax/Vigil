"""Who may start a session, and what stops them.

Every refusal is audited with the reason in it: "who tried to shell into a
machine and why did Vigil say no" is the question an operator asks afterwards,
and the answer has to be in the log rather than in the request body nobody kept.
"""

import re
import secrets
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpResponseNotAllowed, JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.accounts.permissions import OWNER, IsAdmin, can, role_of
from apps.accounts.totp import require_totp_confirmation
from apps.hosts.authentication import authenticate_agent
from apps.hosts.models import Host
from vigil import scoping
from vigil.licensing import has_feature, upgrade_body

from . import bus
from .models import RemoteSession
from .tickets import make_ticket

#: The agent's long-poll. Short enough that an agent which dies mid-wake frees
#: nothing it was holding for long; long enough not to spin on an idle host.
WAKE_SECONDS = 25.0

NO_TOTP = "Set up two-factor authentication before opening a session."
#: What the Connect menu says when an item cannot be used. Each names the one
#: thing that would make it usable, in the same order _start() checks — so a
#: greyed item never blames the wrong cause.
PERM_DENIED = "You don't have permission to open shells here."
MONITORED = "This host is in monitor mode."
OPT_IN_REASON = {
    RemoteSession.Kind.RDP: "Turn on allow_remote_desktop in this host's agent.yml.",
    "container": "Turn on allow_remote_shell in this host's agent.yml.",
    "shell": "Turn on allow_remote_shell in this host's agent.yml.",
}
CONTAINER_NOT_LINUX = "Container shells need a Linux host."
RDP_NOT_WINDOWS = "Web RDP is for Windows hosts."
RDP_NO_GUACD = "Start the guacd service to use Web RDP (see the wiki)."
#: A container name goes into the agent's argv, never a shell — but anything
#: that still reads as option syntax there would be a flag, so it starts with a
#: letter or digit and holds only characters no shell, path or flag can abuse.
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

MAX_ACTIVE_PER_USER = 3


def _audit(user, action: str, target: str, **detail) -> None:
    """Audit a session event. Core must not hard-depend on apps_business."""
    try:
        from apps_business.audits.models import record
    except ImportError:
        return
    record(action, user=user if getattr(user, "is_authenticated", False) else None,
           target=target, **detail)


def _refused(request, host, status: int, reason: str, body: dict,
             kind: str = "", container: str = "") -> Response:
    _audit(request.user, "remote.session_refused",
           getattr(host, "hostname", "") or str(host), reason=reason,
           kind=kind, container=container[:128])
    return Response(body, status=status)


def _has_totp(user) -> bool:
    profile = getattr(user, "profile", None)
    return bool(profile and profile.totp_confirmed_at and profile.totp_secret)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def session_collection(request):
    """POST starts a session; GET lists the recent ones on visible hosts."""
    if request.method == "POST":
        return _start(request)
    return _list(request)


def _start(request):
    data = request.data if isinstance(request.data, dict) else {}
    host, denied = scoping.host_or_404(request, data.get("host"))
    if denied is not None:
        return _refused(request, None, 404, "unknown host",
                        {"error": "Not found"})

    kind = str(data.get("kind") or "")
    # Validated whole: truncating first would let an over-long name through.
    container = str(data.get("container") or "")

    if not can(request.user, scoping.scope_of(host), "hosts", "shell"):
        return _refused(request, host, 403, "no hosts:shell capability",
                        {"error": "Not permitted"}, kind, container)
    if kind not in RemoteSession.Kind.values:
        return _refused(request, host, 400, f"unknown kind {kind!r}",
                        {"error": "Unknown session kind"}, kind, container)
    if kind == RemoteSession.Kind.CONTAINER and not CONTAINER_NAME.match(container):
        return _refused(request, host, 400, f"invalid container name {container!r}",
                        {"error": "Invalid container name"}, kind, container)
    if host.mode == host.Mode.MONITOR:
        return _refused(request, host, 400, "host is in monitor mode",
                        {"error": "This host is in monitor mode."}, kind, container)

    opt_in = _opt_in_name(kind)
    if not getattr(host, _opt_in_field(kind), False):
        return _refused(
            request, host, 409, f"agent has not opted in ({opt_in})",
            {"error": f"This host does not allow remote sessions. Turn it on in "
                      f"the agent's config ({opt_in}).",
             "opt_in": opt_in}, kind, container)

    if not _has_totp(request.user):
        return _refused(request, host, 403, "user has no TOTP enrolled",
                        {"error": NO_TOTP}, kind, container)
    totp_error = require_totp_confirmation(request.user, data)
    if totp_error is not None:
        return _refused(request, host, 401, f"TOTP: {totp_error}",
                        {"error": totp_error}, kind, container)

    active = RemoteSession.objects.filter(
        user=request.user,
        state__in=[RemoteSession.State.PENDING, RemoteSession.State.OPEN],
    ).count()
    if active >= MAX_ACTIVE_PER_USER:
        return _refused(request, host, 429,
                        f"{active} sessions already open for this user",
                        {"error": f"You already have {MAX_ACTIVE_PER_USER} "
                                  f"sessions open. Close one first."},
                        kind, container)

    session = RemoteSession.objects.create(
        host=host, user=request.user, username=request.user.username,
        kind=kind, container=container if kind == RemoteSession.Kind.CONTAINER else "",
        nonce=secrets.token_hex(16),
        client_ip=request.META.get("REMOTE_ADDR") or None,
    )
    bus.push_sync(bus.WAKE_KEY.format(host_id=host.id), make_ticket(session))
    _audit(request.user, "remote.session_started", host.hostname,
           kind=kind, container=session.container, session=str(session.id))
    return Response({"id": str(session.id), "ws": f"/ws/session/{session.id}/",
                     "kind": kind, "host": host.hostname,
                     # The dialog shows a "Recorded" pill from this, and it has
                     # to agree with what Recorder.open() decided to create:
                     # rdp is guacd's to record, not ours.
                     "recorded": bool(has_feature("audit_log")
                                      and kind != RemoteSession.Kind.RDP)},
                    status=201)


def _list(request):
    from apps.hosts.models import Host

    hosts = scoping.filter_by_site(Host.objects.all(), request.user)
    if (host_id := request.query_params.get("host")):
        hosts = hosts.filter(pk=host_id)
    sessions = RemoteSession.objects.filter(host__in=hosts)[:50]
    # The recording itself is admin-and-Business only; so is admitting it exists.
    may_see = role_of(request.user) in (Role.ADMIN, OWNER) and has_feature("audit_log")
    return Response({"sessions": [_row(s, may_see) for s in sessions]})


def _row(s: RemoteSession, may_see_recording: bool) -> dict:
    return {
        "id": str(s.id),
        "host": str(s.host_id),
        "username": s.username,
        "kind": s.kind,
        "container": s.container,
        "state": s.state,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "opened_at": s.opened_at.isoformat() if s.opened_at else None,
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
        "close_reason": s.close_reason,
        "bytes_in": s.bytes_in,
        "bytes_out": s.bytes_out,
        "has_recording": bool(may_see_recording and s.recording),
    }


def _opt_in_field(kind: str) -> str:
    return ("agent_allow_remote_desktop" if kind == RemoteSession.Kind.RDP
            else "agent_allow_remote_shell")


def _host_scope(request, host_id):
    """(host, None) when the caller may see the host, else (None, a 404
    Response). A malformed id answers the same way as one that does not exist:
    neither says anything about what is installed here.
    """
    from django.core.exceptions import ValidationError

    try:
        return scoping.host_or_404(request, host_id)
    except (ValidationError, ValueError):
        return None, Response({"error": "Not found"}, status=404)


def _blocker(user, site, host, kind, is_windows) -> str:
    """The first reason this user cannot start this kind of session on this
    host, in _start()'s order; "" when nothing stops them. The OS checks come
    just after monitor mode: on a Windows box there is no container shell to
    opt in, and reporting a missing agent switch that would not help would send
    the operator to the wrong file.
    """
    if not can(user, site, "hosts", "shell"):
        return PERM_DENIED
    if host.mode == Host.Mode.MONITOR:
        return MONITORED
    if kind == RemoteSession.Kind.RDP and not is_windows:
        return RDP_NOT_WINDOWS
    if kind == RemoteSession.Kind.CONTAINER and is_windows:
        return CONTAINER_NOT_LINUX
    if not getattr(host, _opt_in_field(kind), False):
        return OPT_IN_REASON[kind]
    if not _has_totp(user):
        return NO_TOTP
    if kind == RemoteSession.Kind.RDP and not getattr(settings, "VIGIL_GUACD_ADDR", ""):
        return RDP_NO_GUACD
    return ""


def _item(user, site, host, kind, is_windows) -> dict:
    reason = _blocker(user, site, host, kind, is_windows)
    return {"available": not reason, "reason": reason}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session_options(request):
    """What the Connect menu may offer for one host, and why not otherwise.

    The same checks _start() runs, in the same order: a menu item that lies
    about being usable costs the operator a TOTP code to find out.
    """
    host, denied = _host_scope(request, request.query_params.get("host"))
    if denied is not None:
        return denied
    site = scoping.scope_of(host)
    is_windows = "windows" in (host.os or "").lower()
    return Response({
        "host": host.hostname,
        "is_windows": is_windows,
        "ssh_target": host.ip_address or host.hostname,
        "recorded": has_feature("audit_log"),
        "shell": _item(request.user, site, host, RemoteSession.Kind.SHELL, is_windows),
        "container": _item(request.user, site, host,
                           RemoteSession.Kind.CONTAINER, is_windows),
        "rdp": _item(request.user, site, host, RemoteSession.Kind.RDP, is_windows),
    })


def _opt_in_name(kind: str) -> str:
    return ("allow_remote_desktop" if kind == RemoteSession.Kind.RDP
            else "allow_remote_shell")


def _visible_session(request, session_id):
    """The session, if it is on a host this user can see; else None.

    Every per-session view starts here, so a site-scoped user can neither read
    nor end a session in a site they are not part of, and cannot tell one
    exists there.
    """
    from apps.hosts.models import Host

    hosts = scoping.filter_by_site(Host.objects.all(), request.user)
    return (RemoteSession.objects.filter(pk=session_id, host__in=hosts)
            .select_related("host").first())


def _admin_for(user, session) -> bool:
    """Admin (or owner) in the site of the session's host, not just anywhere."""
    return role_of(user, scoping.scope_of(session.host)) in (Role.ADMIN, OWNER)


@api_view(["GET"])
@permission_classes([IsAdmin])
def session_recording(request, session_id):
    """The recorded output — Business only, and only what recording kept."""
    if not has_feature("audit_log"):
        return Response(upgrade_body("audit_log"), status=402)
    from django.conf import settings
    from django.http import FileResponse

    session = _visible_session(request, session_id)
    if session is None or not _admin_for(request.user, session) or not session.recording:
        return Response({"error": "No recording"}, status=404)
    root = (Path(settings.MEDIA_ROOT) / "recordings").resolve()
    path = (Path(settings.MEDIA_ROOT) / session.recording).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return Response({"error": "No recording"}, status=404)
    return FileResponse(path.open("rb"), content_type="application/x-asciicast")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def session_close(request, session_id):
    """Close a session from the dashboard. Its owner or an admin — nobody else
    can interrupt a colleague's live shell."""
    session = _visible_session(request, session_id)
    if session is None:
        return Response({"error": "Not found"}, status=404)
    if session.user_id != request.user.id and not _admin_for(request.user, session):
        return Response({"error": "Not permitted"}, status=403)
    reason = f"closed by {request.user.username}"
    item = {"t": "close", "reason": reason}
    bus.push_sync(bus.B2A_KEY.format(sid=session.id), item)
    bus.push_sync(bus.A2B_KEY.format(sid=session.id), item)
    return Response({"status": reason}, status=202)


async def agent_wake(request):
    """The agent's long-poll for session tickets.

    A plain async view, not DRF: it runs on the ASGI path and holds a socket
    open for up to ``WAKE_SECONDS`` — which is precisely what a synchronous
    worker could not afford (see the Dockerfile's switch to Uvicorn workers).
    """
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    host, err = await sync_to_async(authenticate_agent, thread_sensitive=True)(request)
    if err is not None:
        return JsonResponse(err.data, status=err.status_code)

    key = bus.WAKE_KEY.format(host_id=host.id)
    tickets = [t for t in await bus.drain(key) if _fresh(t)]
    if not tickets:
        got = await bus.pop(key, WAKE_SECONDS)
        if got is not None and _fresh(got):
            tickets = [got]
    return JsonResponse({"sessions": tickets})


def _fresh(ticket) -> bool:
    import time

    return isinstance(ticket, dict) and ticket.get("exp", 0) > time.time()
