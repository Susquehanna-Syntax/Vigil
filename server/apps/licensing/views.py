"""License API — the one endpoint the frontend renders upgrade state from
(§5: the UI never guesses; it asks). GET is any signed-in user (the greyed
panels need it); POST (paste) is admin-only."""

import threading

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil import licensing
from vigil.editions import FEATURE_TIERS


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def license_view(request):
    if request.method == "POST":
        if not IsAdmin().has_permission(request, None):
            return Response({"detail": IsAdmin.message}, status=403)
        state = licensing.set_license(request.data.get("license", ""))
        if state.status is licensing.Status.INVALID:
            return Response(
                {"detail": f"That license does not verify: {state.detail}",
                 "status": state.status.value},
                status=400,
            )
        if state.business_active and licensing.refresh_enabled():
            # Claim it with Mercantil now, not weeks from now when it nears
            # expiry: until this install claims it, a leaked copy could.
            _refresh_in_background()
    return Response(_payload())


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def license_refresh(request):
    """Refresh now: after a renewal, or to see why the last one failed."""
    outcome = licensing.refresh_license(force=True)
    return Response({**_payload(), "refresh_now": outcome})


def _refresh_in_background():
    """Celery when it is there; a thread otherwise (as the catalog sync does)."""
    from .tasks import refresh_license
    try:
        refresh_license.delay()
        return
    except Exception:  # noqa: BLE001 — no broker: run it here instead
        pass
    threading.Thread(target=refresh_license, daemon=True).start()


def _refresh_status() -> dict:
    from apps.licensing.models import InstallKey
    row = InstallKey.objects.order_by("created_at").first()
    return {
        "enabled": licensing.refresh_enabled(),
        "at": row.refreshed_at.isoformat() if row and row.refreshed_at else None,
        "result": row.refresh_result if row else "",
        "detail": row.refresh_detail if row else "",
    }


def _payload():
    state = licensing.current_state()
    claims = state.claims
    return {
        "tier": state.tier,
        "status": state.status.value,
        "detail": state.detail,
        "source": state.source,
        "instance": licensing.instance_id(),
        "org": claims.org if claims else None,
        "license_id": claims.lid if claims else None,
        "expires": claims.exp if claims else None,
        "seats": {"used": licensing.seats_used(),
                  "allowed": licensing.seats_allowed()},
        "features": [
            {
                "name": name,
                "tier": tier,
                "active": licensing.has_feature(name),
            }
            for name, tier in sorted(FEATURE_TIERS.items())
        ],
        "banners": licensing.banners(),
        "refresh": _refresh_status(),
        "subscribe_url": "https://susquehannasyntax.com/subscribe",
    }
