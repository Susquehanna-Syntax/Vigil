"""Private registry credentials (M11): admin CRUD (the password is write-only)
and the agent's own lookup, limited to the hosts each credential covers."""

import re

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.crypto import decrypt_secret, encrypt_secret

from .models import RegistryCredential

_REGISTRY = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,252})(:[0-9]{1,5})?$")


def _row(c: RegistryCredential) -> dict:
    return {"id": str(c.id), "registry": c.registry, "username": c.username,
            "host_tags": c.host_tags, "created_at": c.created_at.isoformat()}


def covers(cred: RegistryCredential, host) -> bool:
    tags = {str(t).lower() for t in cred.host_tags or []}
    return not tags or bool(tags & {t.key for t in host.tag_rows.all()})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def registry_index(request):
    if request.method == "GET":
        return Response({"results": [_row(c) for c in RegistryCredential.objects.all()]})
    registry = str(request.data.get("registry") or "").strip().lower()
    username = str(request.data.get("username") or "").strip()
    password = str(request.data.get("password") or "")
    tags = request.data.get("host_tags") or []
    if not _REGISTRY.match(registry):
        return Response({"detail": "registry must be a host name, e.g. ghcr.io or docker.io"},
                        status=status.HTTP_400_BAD_REQUEST)
    if not username or not password:
        return Response({"detail": "username and password are required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(tags, list):
        return Response({"detail": "host_tags must be a list"}, status=status.HTTP_400_BAD_REQUEST)
    cred = RegistryCredential.objects.create(
        registry=registry, username=username[:255], password_encrypted=encrypt_secret(password),
        host_tags=[str(t).strip() for t in tags if str(t).strip()], created_by=request.user)
    return Response(_row(cred), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def registry_detail(request, cred_id):
    get_object_or_404(RegistryCredential, pk=cred_id).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AllowAny])
def agent_registry_auth(request):
    """``?registry=ghcr.io`` → the login for this host, or 404."""
    from apps.hosts.authentication import authenticate_agent

    host, err = authenticate_agent(request)
    if err:
        return err
    registry = str(request.query_params.get("registry") or "").lower()
    for cred in RegistryCredential.objects.filter(registry=registry):
        if covers(cred, host):
            return Response({"username": cred.username,
                             "password": decrypt_secret(bytes(cred.password_encrypted)),
                             "serveraddress": registry})
    return Response({"detail": "no credential for this registry on this host"},
                    status=status.HTTP_404_NOT_FOUND)
