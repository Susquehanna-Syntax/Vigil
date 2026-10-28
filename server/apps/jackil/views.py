"""What the Settings pane shows once the integration is working.

A form that saves is not proof that anything happened. The last few tickets
Vigil actually opened are, which is why this exists at all.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin, visible_site_ids
from vigil import scoping

from . import client
from .integration_views import TYPES, _owned
from .models import IntegrationKey, JackilTicket


#: Every tone's colour is in the drawer's own stylesheet, so one outside this
#: set draws default text rather than whatever the string names.
_TONES = ("ok", "warn", "bad", "info", "muted")

#: A link list longer than this is noise; the drawer cannot show it anyway.
MAX_ITEMS = 50


def _safe_url(value) -> str:
    """The URL as an anchor's ``href`` may carry it, or "" — a bare string
    from the helpdesk has no business being a javascript: scheme."""
    url = value if isinstance(value, str) else ""
    return url if url.startswith(("http://", "https://")) else ""


def _link_row(entry) -> dict:
    entry = entry if isinstance(entry, dict) else {}
    tone = entry.get("tone", "")
    return {
        "relation": str(entry.get("relation", "")),
        "label": str(entry.get("label", "")),
        "url": _safe_url(entry.get("url")),
        "status": str(entry.get("status", "")),
        "tone": tone if tone in _TONES else "",
    }


def _item_row(entry) -> dict:
    entry = entry if isinstance(entry, dict) else {}
    return {"name": str(entry.get("name", "")), "url": _safe_url(entry.get("url"))}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def related(request):
    """What Jackil has linked to one of Vigil's objects — the drawer's
    "Related in Jackil" section, and any panel a later phase hangs on it.

    Two rules make this safe to put in front of every signed-in user. The
    visibility check runs *before* Jackil is asked, so nothing about a host
    someone cannot see is ever put to another server, and a missing row and a
    hidden one answer identically. And a helpdesk that is down, unconfigured
    or too old is a 200 with the reason in it: a drawer must not break.
    """
    obj_type = request.query_params.get("type", "")
    obj_id = request.query_params.get("id", "")
    if obj_type not in TYPES:
        return Response({"error": "Unknown type"}, status=400)
    if not obj_id:
        return Response({"error": "Missing id"}, status=400)
    if not _owned(obj_type, request.user, [obj_id]).exists():
        return Response({"error": "Not found"}, status=404)

    if not client.enabled():
        return Response({"configured": False, "links": [], "items": []})
    try:
        payload = client.related(obj_type, obj_id)
    except client.JackilError as exc:
        return Response({"configured": True, "available": False,
                         "message": str(exc), "links": [], "items": []})

    links = payload.get("links") if isinstance(payload.get("links"), list) else []
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return Response({"configured": True, "available": True,
                     "links": [_link_row(e) for e in links[:MAX_ITEMS]],
                     "items": [_item_row(e) for e in items[:MAX_ITEMS]]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def jackil_widget(request, kind):
    """One of Jackil's own widgets, for a tile on a Vigil dashboard.

    The browser cannot hold the API key, so the fetch goes through here. A
    kind that is not a ``jackil:<kind>`` is refused before anything is asked
    of Jackil; every other answer comes back 200 with an envelope in it,
    because a tile whose helpdesk is down should say so and not throw.
    """
    from .remote_widgets import is_remote, remote_widget

    if not is_remote(kind):
        return Response({"error": "Not found"}, status=404)
    return Response(remote_widget(kind, request.query_params.dict()))


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
