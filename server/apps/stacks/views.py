"""Managed stacks API (M11) — single host, Free.

The .env never leaves the server unmasked except through ``env/reveal/``,
which asks for a TOTP code. Editing sends each key with a new value, or
``keep: true`` to keep the stored one, so the browser never has to hold the
secrets it is not changing.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from vigil import hooks, scoping

from .models import ManagedStack, StackRevision
from .validation import (
    NAME_RE,
    StackError,
    describe_compose,
    parse_env,
    render_env,
    validate_compose,
)

STACKS_ROOT = "/opt/vigil/stacks"


def _env_pairs(stack_or_rev) -> list[tuple[str, str]]:
    blob = bytes(stack_or_rev.env_encrypted or b"")
    return parse_env(decrypt_secret(blob)) if blob else []


def _row(s: ManagedStack) -> dict:
    return {"id": str(s.id), "host_id": str(s.host_id), "hostname": s.host.hostname,
            "name": s.name, "compose_yaml": s.compose_yaml, "revision": s.revision,
            "working_dir": s.working_dir, "adopted": s.adopted, "compose_file": s.compose_file,
            "adopt_report": s.adopt_report or {},
            "git": ({"url": s.git_url, "branch": s.git_branch, "pin": s.git_pin, "path": s.git_path,
                     "credential_id": str(s.git_credential_id) if s.git_credential_id else None,
                     "commit": (s.revisions.filter(number=s.revision).values_list("git_commit", flat=True)
                                .first() or "")}
                    if s.git_url else None),
            "env": [{"key": k, "set": bool(v)} for k, v in _env_pairs(s)],
            "updated_at": s.updated_at.isoformat() if s.updated_at else None}


def _merge_env(stack: ManagedStack | None, data) -> list[tuple[str, str]]:
    """New pairs from ``env_text`` (whole file) or ``env`` (key-by-key)."""
    if "env_text" in data:
        return parse_env(str(data.get("env_text") or ""))
    entries = data.get("env")
    if not isinstance(entries, list):
        raise StackError("env must be a list of {key, value} or {key, keep: true}")
    current = dict(_env_pairs(stack)) if stack else {}
    pairs, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise StackError("env must be a list of {key, value} or {key, keep: true}")
        key = str(entry.get("key") or "").strip()
        if key in seen:
            raise StackError(f".env key {key} is listed twice")
        seen.add(key)
        if entry.get("keep"):
            if key not in current:
                raise StackError(f".env key {key} has no stored value to keep")
            value = current[key]
        else:
            value = str(entry.get("value") or "")
        if "\n" in value:
            raise StackError(f".env key {key}: a value cannot span lines")
        pairs.append((key, value))
    return parse_env(render_env(pairs))   # same key rules as a pasted file


def _save(request, stack: ManagedStack, data, *, note: str) -> Response | None:
    if stack.git_url and "compose_yaml" in data:
        return Response({"detail": "this stack's compose file comes from Git: change it in the "
                                   "repository and pull"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        if "compose_yaml" in data:
            validate_compose(str(data["compose_yaml"]))
            stack.compose_yaml = str(data["compose_yaml"])
        if "env_text" in data or "env" in data:
            stack.env_encrypted = encrypt_secret(render_env(_merge_env(
                None if stack._state.adding else stack, data)))
    except StackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        if not stack._state.adding:
            stack.revision += 1
        stack.save()
        StackRevision.objects.create(stack=stack, number=stack.revision,
                                     compose_yaml=stack.compose_yaml,
                                     env_encrypted=stack.env_encrypted,
                                     note=note[:200], created_by=request.user)
    hooks.emit("stack_saved", stack=stack, user=request.user, revision=stack.revision)
    return None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_index(request):
    if request.method == "GET":
        qs = scoping.filter_by_site(ManagedStack.objects.select_related("host"), request.user,
                                    path="host__")
        if host_id := request.query_params.get("host"):
            qs = qs.filter(host_id=host_id)
        return Response({"results": [_row(s) for s in qs]})
    host, denied = scoping.host_or_404(request, request.data.get("host_id"))
    if denied:
        return denied
    name = str(request.data.get("name") or "").strip()
    if not NAME_RE.match(name):
        return Response({"detail": "name must be lowercase letters, digits, _ or -"},
                        status=status.HTTP_400_BAD_REQUEST)
    if ManagedStack.objects.filter(host=host, name=name).exists():
        return Response({"detail": f"{host.hostname} already has a stack called {name}"},
                        status=status.HTTP_400_BAD_REQUEST)
    stack = ManagedStack(host=host, name=name, created_by=request.user,
                         working_dir=f"{STACKS_ROOT}/{name}")
    if "git" in request.data:
        return _create_from_git(request, stack)
    if "compose_yaml" not in request.data:
        return Response({"detail": "compose_yaml (or git) is required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if error := _save(request, stack, request.data, note="created"):
        return error
    return Response(_row(stack), status=status.HTTP_201_CREATED)


def _create_from_git(request, stack: ManagedStack) -> Response:
    """A new stack whose compose file comes from a repository: fetch it,
    check it like any compose file, and store revision 1 with its source."""
    from . import git_views, gitsource

    if denied := git_views.credential_step_up(request):
        return denied
    try:
        git_views.apply_git_settings(stack, request.data)
        if "env_text" in request.data or "env" in request.data:
            stack.env_encrypted = encrypt_secret(render_env(_merge_env(None, request.data)))
        git_views.pull(stack, request.user, note="created from Git")
    except (gitsource.GitSourceError, StackError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_row(stack), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_validate(request):
    """Tell the editor what this compose text says — 200 whether it's valid
    or not, so a bad file is an answer and never a failed request."""
    text = str(request.data.get("compose_yaml") or "")
    keys = request.data.get("env_keys")
    env_keys = [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []
    return Response(describe_compose(text, env_keys))


def _stack_or_404(request, stack_id):
    stack = get_object_or_404(ManagedStack.objects.select_related("host"), pk=stack_id)
    if not scoping.host_in_scope(request.user, stack.host):
        return None
    return stack


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_detail(request, stack_id):
    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        return Response(_row(stack))
    if request.method == "DELETE":
        # The stack keeps running on the host; removing it there is a
        # stack_remove task (phase 09). This only forgets Vigil's copy.
        stack.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if "git" in request.data:
        # New repository settings take effect by pulling them.
        from . import git_views, gitsource
        if not stack.git_url:
            return Response({"detail": "a stack edited in Vigil cannot be switched to Git; "
                                       "create a new stack from the repository"},
                            status=status.HTTP_400_BAD_REQUEST)
        if denied := git_views.credential_step_up(request):
            return denied
        try:
            git_views.apply_git_settings(stack, request.data)
            git_views.pull(stack, request.user, note="Git settings changed")
        except (gitsource.GitSourceError, StackError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_row(stack))
    if error := _save(request, stack, request.data, note=str(request.data.get("note") or "edited")):
        return error
    return Response(_row(stack))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_revisions(request, stack_id):
    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    return Response({"results": [{
        "number": r.number, "note": r.note, "compose_yaml": r.compose_yaml, "git_commit": r.git_commit,
        "env_keys": [k for k, _v in _env_pairs(r)],
        "created_by": r.created_by.username if r.created_by else None,
        "created_at": r.created_at.isoformat()} for r in stack.revisions.all()[:50]]})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_env_reveal(request, stack_id):
    """The .env in the clear — TOTP, every time."""
    from apps.accounts.totp import require_totp_confirmation

    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"Showing secrets needs confirmation: {error}", "needs_totp": True},
                        status=status.HTTP_401_UNAUTHORIZED)
    hooks.emit("stack_env_revealed", stack=stack, user=request.user)
    return Response({"env": [{"key": k, "value": v} for k, v in _env_pairs(stack)]})


def _dispatch(request, stack: ManagedStack, actions: list[dict], name: str):
    """Build, validate and queue one signed task for the stack's host."""
    import secrets

    import yaml

    from apps.tasks.dispatch import task_params
    from apps.tasks.models import Task, TaskRun
    from apps.tasks.spec import parse_and_validate

    spec = parse_and_validate(yaml.safe_dump({"name": name[:120], "actions": actions}))
    params, risk, expires_at = task_params(spec)
    run = TaskRun.objects.create(name_snapshot=name[:120], requested_by=request.user,
                                 host_count=1, step_count=len(actions))
    return Task.objects.create(host=stack.host, run=run, requested_by=request.user,
                               step_label=name[:120], action="_script", params=params,
                               risk_level=risk, state=Task.State.PENDING, expires_at=expires_at,
                               nonce=secrets.token_hex(32))


def _confirmed(request) -> Response | None:
    from apps.accounts.totp import require_totp_confirmation

    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"This needs confirmation: {error}", "needs_totp": True},
                        status=status.HTTP_401_UNAUTHORIZED)
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_deploy(request, stack_id):
    """Deploy the stack's current revision to its host — a high-risk signed
    task, TOTP. The .env goes as a one-time ticket, never in the task."""
    from datetime import timedelta

    from django.utils.timezone import now

    from .models import EnvTicket

    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    # A stack task runs as root on its host: tasks:run in that host's site (SEC-1).
    from apps.tasks.authz import run_denied
    if refused := run_denied(request.user, [stack.host]):
        return refused
    if denied := _confirmed(request):
        return denied
    source = {}
    if stack.git_url and "stack_source" not in set(stack.host.agent_features or []):
        # An older agent would ignore the source ticket and build with no source.
        return Response({"detail": f"{stack.host.hostname}'s agent is too old for stacks from Git — "
                                   "update the agent first"}, status=status.HTTP_400_BAD_REQUEST)
    if stack.git_url:
        # Tracking a branch means a deploy takes its latest commit; a pinned
        # stack re-fetches the same one (and makes no revision).
        from . import git_views, gitsource
        try:
            git_views.pull(stack, request.user)
            source = git_views.deploy_source_params(stack)
        except (gitsource.GitSourceError, StackError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    params = {"project": stack.name, "compose": stack.compose_yaml,
              "working_dir": stack.working_dir, "revision": stack.revision, **source}
    if stack.compose_file != "compose.yaml":
        params["compose_file"] = stack.compose_file
    if bytes(stack.env_encrypted or b""):
        ticket = EnvTicket.objects.create(stack=stack, revision=stack.revision, host=stack.host,
                                          expires_at=now() + timedelta(hours=EnvTicket.TTL_HOURS))
        params["env_ticket"] = str(ticket.id)
    task = _dispatch(request, stack, [{"id": "deploy", "type": "stack_deploy", "params": params}],
                     f"Deploy stack {stack.name} (r{stack.revision})")
    hooks.emit("stack_deployed", stack=stack, user=request.user, task=task)
    return Response({"task": str(task.id), "run": str(task.run_id)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_remove(request, stack_id):
    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    # A stack task runs as root on its host: tasks:run in that host's site (SEC-1).
    from apps.tasks.authz import run_denied
    if refused := run_denied(request.user, [stack.host]):
        return refused
    if denied := _confirmed(request):
        return denied
    delete_files = bool(request.data.get("delete_files")) and not stack.adopted
    task = _dispatch(request, stack, [{"id": "remove", "type": "stack_remove", "params": {
        "project": stack.name, "working_dir": stack.working_dir, "delete_files": delete_files}}],
        f"Take down stack {stack.name}")
    return Response({"task": str(task.id), "run": str(task.run_id)}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([AllowAny])
def agent_stack_env(request, ticket_id):
    """The agent redeems a deploy's env ticket — once, before it expires,
    and only for its own host."""
    from django.db import transaction as _tx
    from django.utils.timezone import now

    from apps.hosts.authentication import authenticate_agent

    from .models import EnvTicket

    host, err = authenticate_agent(request)
    if err:
        return err
    with _tx.atomic():
        ticket = (EnvTicket.objects.select_for_update().select_related("stack")
                  .filter(pk=ticket_id, host=host).first())
        if ticket is None or ticket.used_at is not None or ticket.expires_at <= now():
            return Response({"detail": "ticket unknown, used or expired"}, status=status.HTTP_404_NOT_FOUND)
        ticket.used_at = now()
        ticket.save(update_fields=["used_at"])
        rev = ticket.stack.revisions.filter(number=ticket.revision).first() or ticket.stack
    return Response({"env": render_env(_env_pairs(rev))})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_adopt(request):
    """Take over a stack already running on a host, where it stands (TOTP).

    Queues a stack_read task; the agent hands the compose file and .env to
    the ticket, and the stack becomes managed — recreating nothing."""
    from datetime import timedelta

    from django.utils.timezone import now

    from .models import AdoptTicket

    host, denied = scoping.host_or_404(request, request.data.get("host_id"))
    if denied:
        return denied
    project = str(request.data.get("project") or "")
    if not NAME_RE.match(project):
        return Response({"detail": "not a compose project name"}, status=status.HTTP_400_BAD_REQUEST)
    if ManagedStack.objects.filter(host=host, name=project).exists():
        return Response({"detail": f"{project} is already managed"}, status=status.HTTP_400_BAD_REQUEST)
    # A stack task runs as root on its host: tasks:run in that host's site (SEC-1).
    from apps.tasks.authz import run_denied
    if refused := run_denied(request.user, [host]):
        return refused
    if denied := _confirmed(request):
        return denied
    ticket = AdoptTicket.objects.create(host=host, project=project, requested_by=request.user,
                                        expires_at=now() + timedelta(hours=AdoptTicket.TTL_HOURS))
    placeholder = ManagedStack(host=host, name=project)
    task = _dispatch(request, placeholder, [{"id": "read", "type": "stack_read", "params": {
        "project": project, "adopt_ticket": str(ticket.id)}}], f"Adopt stack {project}")
    return Response({"ticket": str(ticket.id), "task": str(task.id)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def agent_stack_adopt(request, ticket_id):
    """The agent hands over an adopted stack's files — once, for its host."""
    from django.db import transaction as _tx
    from django.utils.timezone import now

    from apps.hosts.authentication import authenticate_agent
    from apps.hosts.models import ContainerStack

    from .models import AdoptTicket

    host, err = authenticate_agent(request)
    if err:
        return err
    data = request.data
    with _tx.atomic():
        ticket = (AdoptTicket.objects.select_for_update()
                  .filter(pk=ticket_id, host=host).first())
        if ticket is None or ticket.used_at is not None or ticket.expires_at <= now():
            return Response({"detail": "ticket unknown, used or expired"}, status=status.HTTP_404_NOT_FOUND)
        ticket.used_at = now()
        try:
            if str(data.get("project") or "") != ticket.project:
                raise StackError("the files are for a different stack")
            compose = str(data.get("compose") or "")
            validate_compose(compose)
            env_pairs = parse_env(str(data.get("env") or ""))
            compose_file = str(data.get("compose_file") or "compose.yaml")[:100]
            working_dir = str(data.get("working_dir") or "")[:500]
            if not working_dir.startswith("/") or ".." in working_dir.split("/"):
                raise StackError("the stack's folder is not an absolute path")
        except StackError as exc:
            ticket.error = str(exc)[:500]
            ticket.save(update_fields=["used_at", "error"])
            return Response({"detail": ticket.error}, status=status.HTTP_400_BAD_REQUEST)
        ticket.save(update_fields=["used_at"])
        stack = ManagedStack(host=host, name=ticket.project, compose_yaml=compose,
                             env_encrypted=encrypt_secret(render_env(env_pairs)) if env_pairs else b"",
                             working_dir=working_dir, compose_file=compose_file, adopted=True,
                             adopt_report=data.get("hashes") if isinstance(data.get("hashes"), dict) else {},
                             created_by=ticket.requested_by)
        stack.save()
        StackRevision.objects.create(stack=stack, number=1, compose_yaml=compose,
                                     env_encrypted=stack.env_encrypted, note="adopted",
                                     created_by=ticket.requested_by)
        ContainerStack.objects.filter(host=host, project=ticket.project).update(
            ownership=ContainerStack.Ownership.ADOPTED)
    hooks.emit("stack_saved", stack=stack, user=ticket.requested_by, revision=1)
    return Response({"ok": True})
