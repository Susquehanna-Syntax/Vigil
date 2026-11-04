from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def writable_sites(request):
    """The sites a create form may offer this user (scoping.site_for_write)."""
    return Response(scoping.writable_sites(request.user))


def _switchable_sites(user):
    """The sites the header switcher lists: every site *user* may see,
    Global first. Authority, not the current selection, decides the list, so
    a site never disappears from the switcher because it was unticked."""
    from apps.accounts.permissions import visible_site_ids

    mods = scoping._sites_models()
    if mods is None:
        return []
    ids = visible_site_ids(user)
    qs = mods.Site.objects.order_by("-is_global", "name")
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return [{"id": str(s.pk), "name": s.name, "is_global": s.is_global} for s in qs]


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def site_selection(request):
    """The header site switcher (S1 04g): GET what it lists and what is
    ticked; PUT ``{"selected": [ids]}`` or ``{"all": true}``. Own profile
    only, browser sessions only: a site-scoped API key is refused, since the
    selection never applies to one."""
    from apps.accounts.models import UserProfile

    user = request.user
    if getattr(user, "key_site_ids", None) is not None:
        return Response({"detail": "The site switcher is for browser sessions."}, status=403)
    sites = _switchable_sites(user)
    known = {s["id"] for s in sites}
    profile = UserProfile.objects.filter(user=user).first()

    if request.method == "PUT":
        if request.data.get("all") is True:
            chosen = []
        else:
            raw = request.data.get("selected")
            if not isinstance(raw, list) or not raw:
                return Response({"detail": "Tick at least one site, or choose all of them."}, status=400)
            chosen = sorted({str(v) for v in raw})
            unknown = [v for v in chosen if v not in known]
            if unknown:
                return Response({"detail": "That is not one of your sites."}, status=400)
            if set(chosen) == known:
                chosen = []  # every site ticked is "all": new sites then show up too
        if profile is None:
            # Creating one here would hand the user a default role; a user
            # without a profile has nothing to narrow anyway.
            return Response({"detail": "This account has no profile to save the choice on."}, status=409)
        profile.site_selection = chosen
        profile.save(update_fields=["site_selection"])

    stored = [str(v) for v in (profile.site_selection if profile else [])]
    selected = [v for v in stored if v in known]
    return Response({
        "enabled": len(sites) > 1,
        "sites": sites,
        "selected": selected,  # empty: every site
    })
