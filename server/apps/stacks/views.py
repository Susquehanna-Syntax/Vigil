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
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from vigil import hooks, scoping

from .models import ManagedStack, StackRevision
from .validation import NAME_RE, StackError, parse_env, render_env, validate_compose

STACKS_ROOT = "/opt/vigil/stacks"


def _env_pairs(stack_or_rev) -> list[tuple[str, str]]:
    blob = bytes(stack_or_rev.env_encrypted or b"")
    return parse_env(decrypt_secret(blob)) if blob else []


def _row(s: ManagedStack) -> dict:
    return {"id": str(s.id), "host_id": str(s.host_id), "hostname": s.host.hostname,
            "name": s.name, "compose_yaml": s.compose_yaml, "revision": s.revision,
            "working_dir": s.working_dir, "adopted": s.adopted,
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
    if "compose_yaml" not in request.data:
        return Response({"detail": "compose_yaml is required"}, status=status.HTTP_400_BAD_REQUEST)
    stack = ManagedStack(host=host, name=name, created_by=request.user,
                         working_dir=f"{STACKS_ROOT}/{name}")
    if error := _save(request, stack, request.data, note="created"):
        return error
    return Response(_row(stack), status=status.HTTP_201_CREATED)


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
        "number": r.number, "note": r.note, "compose_yaml": r.compose_yaml,
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
