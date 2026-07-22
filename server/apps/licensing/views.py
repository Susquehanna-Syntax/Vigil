"""License API — the one endpoint the frontend renders upgrade state from
(§5: the UI never guesses; it asks). GET is any signed-in user (the greyed
panels need it); POST (paste) is admin-only."""

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
    return Response(_payload())


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
        "subscribe_url": "https://susquehannasyntax.com/subscribe",
    }
