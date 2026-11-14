"""Private registry credentials (M11): admin CRUD (the password is write-only)
and the agent's own lookup, limited to the hosts each credential covers."""

import re

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.crypto import decrypt_secret, encrypt_secret

from .api_schema import (
    StacksErrorSerializer,
    StacksRegistryAuthSerializer,
    StacksRegistryCredentialCreateSerializer,
    StacksRegistryCredentialListSerializer,
    StacksRegistryCredentialSerializer,
)
from .models import RegistryCredential
from vigil import scoping

_REGISTRY = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,252})(:[0-9]{1,5})?$")


def _row(c: RegistryCredential) -> dict:
    return {"id": str(c.id), "registry": c.registry, "username": c.username,
            "host_tags": c.host_tags, "site_id": str(c.site_id) if c.site_id else None,
            "created_at": c.created_at.isoformat()}


def covers(cred: RegistryCredential, host) -> bool:
    """A site's credential covers only that site's hosts (S1 04e): the agent
    endpoint runs unscoped, so without this a West password reached a Lab
    host whose tags matched. Then the tag filter, if any."""
    site = scoping.owning_site_id(cred)
    if site is not None and scoping.site_id_of(host) != site:
        return False
    tags = {str(t).lower() for t in cred.host_tags or []}
    return not tags or bool(tags & {t.key for t in host.tag_rows.all()})


@extend_schema(methods=["GET"], responses={200: StacksRegistryCredentialListSerializer})
@extend_schema(methods=["POST"], request=StacksRegistryCredentialCreateSerializer,
               responses={201: StacksRegistryCredentialSerializer,
                          400: StacksErrorSerializer, 403: StacksErrorSerializer})
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
    site_id, denied = scoping.site_for_write(request.user, request.data.get("site_id"))
    if denied:
        return denied
    cred = RegistryCredential.objects.create(
        registry=registry, username=username[:255], password_encrypted=encrypt_secret(password),
        host_tags=[str(t).strip() for t in tags if str(t).strip()], created_by=request.user,
        site_id=site_id)
    return Response(_row(cred), status=status.HTTP_201_CREATED)


@extend_schema(methods=["DELETE"], request=None, responses={204: None,
                                                            403: StacksErrorSerializer})
@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def registry_detail(request, cred_id):
    cred = get_object_or_404(RegistryCredential, pk=cred_id)
    if denied := scoping.write_denied(request.user, cred):
        return denied
    cred.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@scoping.system_view(reason="agent API: the caller is a host, authenticated by its agent token")
@extend_schema(responses={200: StacksRegistryAuthSerializer})
@api_view(["GET"])
@permission_classes([AllowAny])
def agent_registry_auth(request):
    """``?registry=ghcr.io`` → the login for this host, or 404."""
    from apps.hosts.authentication import authenticate_agent

    host, err = authenticate_agent(request)
    if err:
        return err
    registry = str(request.query_params.get("registry") or "").lower()
    # The host's own site's credential first, then a global one.
    creds = sorted(RegistryCredential.objects.filter(registry=registry),
                   key=lambda c: c.site_id is None)
    for cred in creds:
        if covers(cred, host):
            return Response({"username": cred.username,
                             "password": decrypt_secret(bytes(cred.password_encrypted)),
                             "serveraddress": registry})
    return Response({"detail": "no credential for this registry on this host"},
                    status=status.HTTP_404_NOT_FOUND)
