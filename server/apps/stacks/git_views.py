"""Stacks from Git (2026.14.1): credentials, pulling a repository into a
revision, and the agent's one-time download of a revision's source folder.

The repository is the source of truth for a Git stack's compose file — it is
not edited in Vigil — while its .env still is. See gitsource.py for how the
server talks to the Git server.
"""

import re
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from vigil import hooks

from . import gitsource
from .models import GitCredential, ManagedStack, SourceSnapshot, SourceTicket, StackRevision
from .validation import StackError, validate_compose

#: Snapshots kept per stack; older ones (and their tickets) are pruned.
KEEP_SNAPSHOTS = 5


# ── Credentials ──────────────────────────────────────────────────────────


def _cred_row(c: GitCredential) -> dict:
    return {"id": str(c.id), "name": c.name, "kind": c.kind, "git_host": c.git_host, "username": c.username,
            "has_known_hosts": bool(c.known_hosts.strip()),
            "created_at": c.created_at.isoformat()}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def git_credential_index(request):
    """List (never the secret) or add a Git credential. Adding one asks for
    TOTP: it is a key to a repository whose contents run as root on hosts."""
    if request.method == "GET":
        return Response({"results": [_cred_row(c) for c in GitCredential.objects.all()]})
    from apps.accounts.totp import require_totp_confirmation
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": error, "needs_totp": True}, status=status.HTTP_401_UNAUTHORIZED)
    name = str(request.data.get("name") or "").strip()[:120]
    kind = str(request.data.get("kind") or "")
    secret = str(request.data.get("secret") or "")
    known_hosts = str(request.data.get("known_hosts") or "")
    git_host = str(request.data.get("git_host") or "").strip().lower()
    if not name or kind not in GitCredential.Kind.values or not secret.strip():
        return Response({"detail": "name, kind (token or ssh_key) and secret are required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if not re.fullmatch(gitsource._HOST, git_host):
        return Response({"detail": "git_host must be the Git server's host name, e.g. github.com — "
                                   "the credential is used with that server only"},
                        status=status.HTTP_400_BAD_REQUEST)
    if kind == GitCredential.Kind.SSH_KEY:
        if "PRIVATE KEY" not in secret:
            return Response({"detail": "paste the private key (-----BEGIN … PRIVATE KEY-----)"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not known_hosts.strip():
            return Response({"detail": "an SSH key needs the Git server's host key (a known_hosts "
                                       "line, e.g. from ssh-keyscan) — Vigil does not trust on first use"},
                            status=status.HTTP_400_BAD_REQUEST)
    if len(secret) > 20_000 or len(known_hosts) > 20_000:
        return Response({"detail": "that is too long for a credential"},
                        status=status.HTTP_400_BAD_REQUEST)
    cred = GitCredential.objects.create(
        name=name, kind=kind, git_host=git_host, username=str(request.data.get("username") or "").strip()[:255],
        secret_encrypted=encrypt_secret(secret), known_hosts=known_hosts.strip(),
        created_by=request.user)
    hooks.emit("git_credential_added", credential=cred, user=request.user)
    return Response(_cred_row(cred), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def git_credential_detail(request, cred_id):
    get_object_or_404(GitCredential, pk=cred_id).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Pulling ──────────────────────────────────────────────────────────────


def source_for(stack: ManagedStack) -> gitsource.Source:
    cred = None
    if stack.git_credential_id:
        c = stack.git_credential
        cred = (c.kind, c.username, decrypt_secret(bytes(c.secret_encrypted)), c.known_hosts,
                c.git_host)
    return gitsource.Source(url=stack.git_url, branch=stack.git_branch or "main",
                            pin=stack.git_pin, path=stack.git_path or "compose.yaml",
                            credential=cred)


def apply_git_settings(stack: ManagedStack, data) -> None:
    """Copy a request's ``git`` block onto *stack*, checked. Raises
    GitSourceError."""
    git = data.get("git")
    if not isinstance(git, dict):
        raise gitsource.GitSourceError("git must be an object with url, branch, pin, path")
    url = str(git.get("url") or "").strip()
    gitsource.parse_url(url)
    branch = gitsource.check_ref(str(git.get("branch") or "main"), "branch")
    pin = str(git.get("pin") or "").strip()
    if pin and not gitsource._SHA_RE.match(pin.lower()):
        gitsource.check_ref(pin, "pin")
    path = str(git.get("path") or "compose.yaml").strip()
    gitsource.check_path(path)
    cred_id = git.get("credential_id") or None
    cred = None
    if cred_id:
        cred = GitCredential.objects.filter(pk=cred_id).first()
        if cred is None:
            raise gitsource.GitSourceError("that Git credential does not exist")
        _kind, host = gitsource.parse_url(url)
        if cred.git_host != host:
            raise gitsource.GitSourceError(f"the credential {cred.name!r} is for {cred.git_host}, not {host}")
    stack.git_url, stack.git_branch, stack.git_pin, stack.git_path = url, branch, pin, path
    stack.git_credential = cred


def pull(stack: ManagedStack, user, *, note: str = "") -> bool:
    """Fetch the stack's repository; when it brings a new commit (or a
    changed compose file), make it a new revision with its source snapshot.
    Returns whether a revision was made. Raises GitSourceError / StackError."""
    fetched = gitsource.fetch(source_for(stack))
    validate_compose(fetched.compose_yaml)
    current = stack.revisions.filter(number=stack.revision).first() if not stack._state.adding else None
    if (current is not None and current.git_commit == fetched.commit
            and current.compose_yaml == fetched.compose_yaml
            and stack.snapshots.filter(revision=stack.revision, sha256=fetched.sha256).exists()):
        return False
    with transaction.atomic():
        if not stack._state.adding:
            stack.revision += 1
        stack.compose_yaml = fetched.compose_yaml
        stack.compose_file = fetched.compose_file
        stack.save()
        StackRevision.objects.create(
            stack=stack, number=stack.revision, compose_yaml=stack.compose_yaml,
            env_encrypted=stack.env_encrypted, git_commit=fetched.commit,
            note=(note or f"from Git {fetched.commit[:12]}")[:200], created_by=user)
        SourceSnapshot.objects.create(stack=stack, revision=stack.revision, commit=fetched.commit,
                                      sha256=fetched.sha256, size=len(fetched.archive),
                                      data=fetched.archive)
        stale = list(stack.snapshots.order_by("-created_at").values_list("id", flat=True)[KEEP_SNAPSHOTS:])
        SourceSnapshot.objects.filter(id__in=stale).delete()
    hooks.emit("stack_saved", stack=stack, user=user, revision=stack.revision)
    return True


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stack_pull(request, stack_id):
    """Fetch the repository now and make a revision if it changed."""
    from .views import _row, _stack_or_404

    stack = _stack_or_404(request, stack_id)
    if stack is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    if not stack.git_url:
        return Response({"detail": "this stack's compose file is edited in Vigil, not pulled from Git"},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        changed = pull(stack, request.user)
    except (gitsource.GitSourceError, StackError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"changed": changed, "stack": _row(stack)})


def deploy_source_params(stack: ManagedStack) -> dict:
    """Task params that let the host fetch this revision's folder once:
    a ticket, and the sha256 the archive must have."""
    snap = stack.snapshots.filter(revision=stack.revision).order_by("-created_at").first()
    if snap is None:
        raise gitsource.GitSourceError("this revision has no source from Git yet — pull it first")
    ticket = SourceTicket.objects.create(snapshot=snap, host=stack.host,
                                         expires_at=now() + timedelta(hours=SourceTicket.TTL_HOURS))
    return {"source_ticket": str(ticket.id), "source_sha256": snap.sha256,
            "source_commit": snap.commit}


# ── The agent's download ─────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([AllowAny])
def agent_stack_source(request, ticket_id):
    """The agent redeems a deploy's source ticket — once, before it expires,
    and only for its own host. The archive's sha256 is in the signed task."""
    from apps.hosts.authentication import authenticate_agent

    host, err = authenticate_agent(request)
    if err:
        return err
    with transaction.atomic():
        ticket = (SourceTicket.objects.select_for_update().select_related("snapshot")
                  .filter(pk=ticket_id, host=host).first())
        if ticket is None or ticket.used_at is not None or ticket.expires_at <= now():
            return Response({"detail": "ticket unknown, used or expired"}, status=status.HTTP_404_NOT_FOUND)
        ticket.used_at = now()
        ticket.save(update_fields=["used_at"])
        data = bytes(ticket.snapshot.data)
    response = HttpResponse(data, content_type="application/gzip")
    response["Content-Length"] = str(len(data))
    response["Cache-Control"] = "no-store"
    return response


def credential_step_up(request) -> Response | None:
    """Attaching a credential to a stack decides which server a secret is
    sent to: it asks for TOTP, like every other action that releases one."""
    git = request.data.get("git")
    if not (isinstance(git, dict) and git.get("credential_id")):
        return None
    from apps.accounts.totp import require_totp_confirmation
    if error := require_totp_confirmation(request.user, request.data):
        return Response({"detail": f"Using a Git credential needs confirmation: {error}", "needs_totp": True},
                        status=status.HTTP_401_UNAUTHORIZED)
    return None
