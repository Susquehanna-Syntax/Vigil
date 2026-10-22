"""Redeeming a config token. Unauthenticated by necessity.

The installer that calls here holds no credentials — it is trying to obtain the
config that lets it get them. Security is the unguessable one-time token, so
the redeem is a conditional UPDATE whose rowcount is the lock, and anything
that is not exactly one live match answers 404.
"""
import hashlib
import re

from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle

from .models import EnrollConfig
from .render import to_yaml

#: A token is 32 url-safe random bytes. Anything longer or with other
#: characters is not one we minted, so it never reaches the database.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class _ConfigThrottle(AnonRateThrottle):
    """Per-IP cap, mirroring the throttle on hosts.register()."""

    scope = "agent_enroll_config"
    rate = "60/hour"


def _miss() -> Http404:
    return Http404("Unknown or used config token.")


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([_ConfigThrottle])
def redeem(request, token: str):
    if not TOKEN_RE.match(token or ""):
        raise _miss()

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with transaction.atomic():
        # The rowcount is the lock: two installers racing on one token means
        # exactly one of them gets a config.
        claimed = EnrollConfig.objects.filter(
            token_hash=token_hash,
            used_at__isnull=True,
            expires_at__gt=now(),
        ).update(used_at=now(), used_ip=request.META.get("REMOTE_ADDR"))
        if not claimed:
            raise _miss()
        settings = EnrollConfig.objects.values_list(
            "settings", flat=True).get(token_hash=token_hash)

    response = HttpResponse(to_yaml(settings), content_type="text/yaml; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response
