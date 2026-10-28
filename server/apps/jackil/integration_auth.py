"""Bearer authentication for a Jackil integration key.

Deliberately *not* in ``REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]``.
It is listed on the integration views alone, which accept nothing else: a key
is a standing read grant, so it must not be able to reach an endpoint written
for a session — and a session must not reach the integration endpoints.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import KEY_PREFIX, IntegrationKey, hash_integration_key

#: One recorded use per key per minute is what the throttle is for:
#: ``last_used_at`` tells an admin whether a key is load-bearing before they
#: revoke it, and that is worth one write a minute — not one per lookup on a
#: helpdesk ticket.
USE_THROTTLE = timedelta(minutes=1)


class IntegrationKeyAuthentication(BaseAuthentication):
    """``Authorization: Bearer vjk_…`` → the key's creator.

    Returning ``None`` for anything that is not a ``vjk_`` bearer token keeps
    the two credential kinds disjoint. Anything that *looks* like our key and
    fails is refused outright rather than passed on, so a rejected key can
    never fall through to whoever's session happens to ride along.
    """

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented.startswith(KEY_PREFIX):
            return None

        key = (IntegrationKey.objects.select_related("created_by")
               .filter(key_hash=hash_integration_key(presented)).first())
        if key is None or not key.is_active or not key.created_by.is_active:
            raise AuthenticationFailed("Invalid integration key")

        _touch(key)
        return key.created_by, key

    def authenticate_header(self, request):
        # Non-empty makes DRF answer 401 with this challenge instead of a 403
        # that would confirm the endpoint exists to a caller holding nothing.
        return 'Bearer realm="vigil-jackil"'


def _touch(key) -> None:
    """Record the use, at most once a minute. A conditional UPDATE whose row
    count says whether it was due: one statement, and no write per request."""
    cutoff = timezone.now() - USE_THROTTLE
    due = (IntegrationKey.objects.filter(pk=key.pk, last_used_at__isnull=True)
           | IntegrationKey.objects.filter(pk=key.pk, last_used_at__lt=cutoff))
    if due.update(last_used_at=timezone.now()):
        key.last_used_at = timezone.now()
