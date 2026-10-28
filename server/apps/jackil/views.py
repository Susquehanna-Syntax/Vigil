"""What the Settings pane shows once the integration is working.

A form that saves is not proof that anything happened. The last few tickets
Vigil actually opened are, which is why this exists at all.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin, visible_site_ids
from vigil import scoping

from .models import IntegrationKey, JackilTicket


@api_view(["GET"])
@permission_classes([IsAdmin])
def recent_tickets(request):
    # Scoped by site, like the alert list these rows came from. IsAdmin asks
    # the unscoped question — role_of(user) answers with a scoped user's
    # strongest role anywhere — so an admin of one site passes it. Without
    # this they would then read every site's hostnames and alert text, which
    # is precisely what a site boundary is for. A ticket hangs off an alert
    # which hangs off a host, hence the two-step path; membership semantics
    # (cascade_global=False) match Alert, because a ticket is about one host
    # rather than a rule that applies everywhere.
    visible = scoping.filter_by_site(
        JackilTicket.objects.all(), request.user, path="alert__host__")
    rows = (visible
            .select_related("alert", "alert__host")
            .order_by("-created_at")[:5])
    return Response({
        "count": visible.count(),
        "tickets": [{
            "ticket_id": row.ticket_id,
            "url": row.url,
            "host": row.alert.host.hostname,
            "severity": row.alert.severity,
            "message": row.alert.message,
            "created_at": row.created_at,
            "cleared_at": row.cleared_at,
        } for row in rows],
    })


def _key_row(row) -> dict:
    """A key as the admin list shows it. Deliberately no ``key_hash``, and the
    plaintext is never in a row at all — this is the shape that can be sent."""
    return {
        "id": row.pk,
        "name": row.name,
        "prefix": row.prefix,
        "created_by": row.created_by.username,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
    }


def _visible_keys(user):
    """Keys *user* may see. Every key is the unscoped admin's business — they
    are the ones who notice an abandoned one and revoke it — while a
    site-scoped admin is not told that another site has integrations at all."""
    keys = IntegrationKey.objects.select_related("created_by").order_by("-created_at")
    if visible_site_ids(user) is None:
        return keys
    return keys.filter(created_by=user)


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def integration_keys(request):
    """List the visible keys, or mint one. The mint response is the only
    place the plaintext ever appears; a list never carries a ``key``."""
    if request.method == "GET":
        return Response({"keys": [_key_row(k) for k in _visible_keys(request.user)]})

    name = str(request.data.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        return Response({"error": "Name must be between 1 and 80 characters"}, status=400)
    key, raw = IntegrationKey.mint(name=name, user=request.user)
    return Response({**_key_row(key), "key": raw}, status=201)


@api_view(["POST"])
@permission_classes([IsAdmin])
def revoke_integration_key(request, pk: int):
    """Revoke a key: 404 for one the caller cannot see, so the answer to
    "does that id exist somewhere else" is the same as the answer to "no".
    Idempotent — revoking twice is what a double-click does."""
    key = _visible_keys(request.user).filter(pk=pk).first()
    if key is None:
        return Response({"error": "Not found"}, status=404)
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
    return Response(_key_row(key))
