"""The relays API.

Admin-only — a relay's URL and key decide where agents' traffic goes, and
turning off the built-in relay decides whether the dashboard accepts agents at
all. The link key is shown exactly once, on create or rotate; every other read
strips it.
"""

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.accounts.totp import require_totp_confirmation
from vigil.licensing import relays_allowed, upgrade_body

from .models import Relay, enabled_count, generate_key, set_key


def _row(relay: Relay) -> dict:
    return {
        "id": str(relay.id),
        "name": relay.name,
        "location": relay.location,
        "url": relay.url,
        "kind": relay.kind,
        "enabled": relay.enabled,
        "connected": relay.connected,
        "last_seen_at": relay.last_seen_at.isoformat() if relay.last_seen_at else None,
        "last_error": relay.last_error,
        "created_at": relay.created_at.isoformat() if relay.created_at else None,
    }


def _audit(request, action: str, target: str) -> None:
    """Audit a mutation. Core must not hard-depend on apps_business."""
    try:
        from apps_business.audits.models import record
    except ImportError:
        return
    record(action, user=request.user, target=target)


def _clean_url(raw) -> tuple[str | None, str | None]:
    """(url, error). https only unless DEBUG; no whitespace; one trailing / off."""
    url = str(raw or "").strip()
    if len(url) > 300:
        return None, "url must be at most 300 characters"
    if any(c.isspace() for c in url):
        return None, "url must not contain whitespace"
    allowed = ("https://", "http://") if settings.DEBUG else ("https://",)
    if not url.startswith(allowed):
        return None, "url must start with https://"
    return url.rstrip("/"), None


def _over_limit() -> Response:
    body = upgrade_body("relays")
    body["allowed"] = relays_allowed()
    body["used"] = enabled_count()
    return Response(body, status=402)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def relay_index(request):
    if request.method == "GET":
        return Response({
            "allowed": relays_allowed(),
            "used": enabled_count(),
            "relays": [_row(relay) for relay in Relay.objects.all()],
        })

    payload = request.data or {}
    if err := require_totp_confirmation(request.user, payload):
        return Response({"detail": err}, status=400)

    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 80:
        return Response({"detail": "name must be 1-80 characters"}, status=400)
    url, problem = _clean_url(payload.get("url"))
    if problem:
        return Response({"detail": problem}, status=400)
    enabled = bool(payload.get("enabled", True))
    if enabled and enabled_count() + 1 > relays_allowed():
        return _over_limit()

    relay = Relay(name=name, location=str(payload.get("location") or "").strip()[:120],
                  url=url, kind=Relay.Kind.REMOTE, enabled=enabled)
    key = generate_key()
    set_key(relay, key)
    relay.save()
    _audit(request, "relay.created", relay.name)
    row = _row(relay)
    row["key"] = key
    row["key_sha256"] = relay.key_sha256
    return Response(row, status=201)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def relay_detail(request, relay_id):
    relay = get_object_or_404(Relay, pk=relay_id)
    payload = request.data or {}

    # A relay's url is the fallback every agent is handed, and enabled decides
    # whether agents get in at all (the built-in relay) — the same weight as
    # creating one, so the same second factor. Renaming needs none.
    if request.method == "DELETE" or "url" in payload or "enabled" in payload:
        if err := require_totp_confirmation(request.user, payload):
            return Response({"detail": err}, status=400)

    if request.method == "DELETE":
        if relay.kind == Relay.Kind.BUILTIN:
            return Response({"detail": "The built-in relay cannot be deleted."},
                            status=400)
        name = relay.name
        relay.delete()
        _audit(request, "relay.deleted", name)
        return Response(status=204)

    changes: dict = {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not 1 <= len(name) <= 80:
            return Response({"detail": "name must be 1-80 characters"}, status=400)
        changes["name"] = name
    if "location" in payload:
        changes["location"] = str(payload.get("location") or "").strip()[:120]
    if "url" in payload and relay.kind == Relay.Kind.REMOTE:
        url, problem = _clean_url(payload.get("url"))
        if problem:
            return Response({"detail": problem}, status=400)
        changes["url"] = url

    if "enabled" in payload:
        want = bool(payload.get("enabled"))
        if want and not relay.enabled and enabled_count() + 1 > relays_allowed():
            return _over_limit()
        if not want and relay.kind == Relay.Kind.BUILTIN:
            # Turning off direct connections is only safe once a relay has
            # actually proven it can carry agents.
            connected = Relay.objects.filter(
                kind=Relay.Kind.REMOTE, enabled=True,
                last_seen_at__isnull=False).exists()
            if not connected:
                return Response(
                    {"detail": "Enable a remote relay that has connected at least "
                               "once before turning off direct connections."},
                    status=400)
        changes["enabled"] = want

    for field, value in changes.items():
        setattr(relay, field, value)
    if changes:
        relay.save(update_fields=[*changes])
    _audit(request, "relay.updated", relay.name)
    return Response(_row(relay))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def relay_rotate_key(request, relay_id):
    relay = get_object_or_404(Relay, pk=relay_id)
    if relay.kind == Relay.Kind.BUILTIN:
        return Response({"detail": "The built-in relay has no key."}, status=400)
    if err := require_totp_confirmation(request.user, request.data):
        return Response({"detail": err}, status=400)

    key = generate_key()
    set_key(relay, key)
    relay.connected = False
    relay.save(update_fields=["key_encrypted", "key_sha256", "connected"])
    _audit(request, "relay.key_rotated", relay.name)
    return Response({"key": key, "key_sha256": relay.key_sha256})
