"""Write authority: who may change or create an object, and in which site
(S1 04e). Reads are the managers' job; writes are decided here, by the
object's own site. ``vigil.scoping`` re-exports everything here as before.
"""
from vigil import scoping as _pkg

from .sites import (
    _assignment_for,
    _global_site_id,
    site_id_of,
    site_roles_for,
    site_selection_for,
)

# ── Write rules (S1 04e, decisions-06 items 1-3) ──────────────────────────────
# Reads are the managers' job; writes are decided here, by the object's own
# site. A global object (no site, or the Global site) is changed only with the
# authority of the Global scope: a Global row, or no site rows at all (legacy
# installs, decision 10). A per-site role never reaches it, not even beside a
# Global viewer row, and a site-scoped API key never does. A site's object is
# changed with the role the user holds in that site.

def owning_site_id(obj):
    """The id of the site that owns *obj*, or None when it is global."""
    try:
        obj._meta.get_field("site_id")
    except Exception:  # noqa: BLE001 — FieldDoesNotExist: an assignment model
        return site_id_of(obj)
    sid = obj.site_id
    mods = _pkg._sites_models()
    if sid is None or mods is None:
        return None
    return None if sid == _global_site_id(mods) else sid


def _write_site(user, site_id):
    """The site to ask ``can()`` about for a write in *site_id* (None =
    global), or False when the write is refused outright."""
    mods = _pkg._sites_models()
    if site_id is not None:
        return site_id
    if getattr(user, "key_site_ids", None) is not None:
        return False
    glob = _global_site_id(mods)
    if glob is not None:
        return glob
    # No Global site row exists: only a user without site rows holds global
    # authority (role_of(user, None) would otherwise be their strongest role).
    return None if not site_roles_for(user) else False


def _allowed(user, site, app, verb) -> bool:
    from apps.accounts.models import Role
    from apps.accounts.permissions import OWNER, can, role_of

    if app is None:
        return role_of(user, site) in (OWNER, Role.ADMIN)
    return can(user, site, app, verb)


def write_denied(user, obj, app=None, verb=None):
    """None when *user* may change *obj*, else the response to send.

    Without *app*/*verb* the write needs the admin role in the object's site,
    the per-site form of IsAdmin. Another site's object answers 404, as if it
    did not exist; a visible object the user may not change answers 403.
    """
    from rest_framework.response import Response

    from apps.accounts.permissions import visible_site_ids

    if _pkg._sites_models() is None:
        return None
    sid = owning_site_id(obj)
    ids = visible_site_ids(user)
    if sid is not None and ids is not None and sid not in ids:
        return Response({"detail": "Not found"}, status=404)
    site = _write_site(user, sid)
    if site is False or not _allowed(user, site, app, verb):
        if sid is None:
            return Response({"detail": "Global items can be changed only by an administrator "
                                       "of the whole install."}, status=403)
        return Response({"detail": "Your role in this site may not change it."}, status=403)
    return None


def site_for_write(user, raw, app=None, verb=None):
    """``(site_id, None)`` for the site *user* asked to put an object in, or
    ``(None, response)`` when they may not.

    *raw* is the request's ``site_id`` (None or "" = global). A site the user
    cannot see answers 404; global, or a site where their role does not allow
    the write, answers 403. Community installs have no sites: always global.
    """
    import uuid as _uuid

    from rest_framework.response import Response

    from apps.accounts.permissions import visible_site_ids

    mods = _pkg._sites_models()
    if mods is None:
        return None, None
    sid = None
    if raw not in (None, ""):
        try:
            sid = _uuid.UUID(str(raw))
        except ValueError:
            return None, Response({"detail": f"unknown site {raw!r}"}, status=400)
        if sid == _global_site_id(mods):
            sid = None
    if sid is not None:
        ids = visible_site_ids(user)
        if ids is not None and sid not in ids:
            # Unknown and out of reach look the same to a scoped user.
            return None, Response({"detail": "Not found"}, status=404)
        if not mods.Site.objects.filter(pk=sid).exists():
            return None, Response({"detail": f"unknown site {raw!r}"}, status=400)
    site = _write_site(user, sid)
    if site is False or not _allowed(user, site, app, verb):
        if sid is None:
            return None, Response({"detail": "Choose one of your sites: only an administrator of "
                                             "the whole install creates global items."}, status=403)
        return None, Response({"detail": "Your role in this site may not create this."}, status=403)
    return sid, None


def place(obj, site_id):
    """Put an assignment-placed object (playbook, automation, channel) in
    *site_id*, or make it global with None. Callers check site_for_write first."""
    assignment, field = _assignment_for(type(obj))
    if assignment is None:
        return
    if site_id is None:
        assignment.objects.filter(**{field: obj}).delete()
    else:
        assignment.objects.update_or_create(**{field: obj}, defaults={"site_id": site_id})
    obj.__dict__.pop("_vigil_site_id", None)


def with_sites(qs):
    """*qs* with each row's site assignment joined in, so row_site_id costs no
    query per row. Untouched on Community installs."""
    if _assignment_for(qs.model)[0] is None:
        return qs
    return qs.select_related("site_assignment__site")


def row_site_id(obj):
    """*obj*'s site id for an API row, None when global."""
    if _assignment_for(type(obj))[0] is None:
        return None
    try:
        row = obj.site_assignment
    except Exception:  # noqa: BLE001 — RelatedObjectDoesNotExist: no row, global
        return None
    return None if row.site.is_global else row.site_id


def writable_sites(user, app=None, verb=None) -> dict:
    """Where *user* may create things, for a form's site picker: the same
    rules as site_for_write. ``global`` says whether "no site" is allowed;
    ``enabled`` is False when there are no sites to choose between."""
    from apps.accounts.permissions import visible_site_ids

    mods = _pkg._sites_models()
    if mods is None:
        return {"enabled": False, "global": True, "sites": []}
    ids = visible_site_ids(user)
    qs = mods.Site.objects.filter(is_global=False).order_by("name")
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    sites = [{"id": str(site.pk), "name": site.name} for site in qs
             if _allowed(user, site.pk, app, verb)]
    # The sites ticked in the header switcher come first (S1 04g), so a
    # form's default is a site the user is looking at.
    chosen = {str(pk) for pk in (site_selection_for(user) or ())}
    sites.sort(key=lambda s: s["id"] not in chosen)
    glob = _write_site(user, None)
    may_global = glob is not False and _allowed(user, glob, app, verb)
    return {"enabled": bool(sites), "global": may_global, "sites": sites}


def host_in_scope(user, host, app=None) -> bool:
    """May `user` reach this host at all? False means the caller should 404
    rather than 403 — a scoped user should not learn that a host exists in a
    site they cannot see. *app* lets a company-wide grant on that module count."""
    from apps.accounts.permissions import visible_site_ids

    ids = visible_site_ids(user, app=app)
    if ids is None:
        return True
    if not ids:
        return False

    mods = _pkg._sites_models()
    if mods is None:
        return True

    row = mods.HostSiteAssignment.objects.filter(host=host).first()
    if row is not None:
        return row.site_id in ids
    # Unassigned means Global.
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    return glob in ids


def visible_host(user, host_id):
    """The host with this id, or None when *user* may not reach it.

    Returning None for "does not exist" and "exists in a site you cannot see"
    alike is deliberate: callers answer 404 either way, so a scoped operator
    cannot learn that a host exists elsewhere by probing ids.

    Every per-host endpoint should route its lookup through this. The host list
    has always been narrowed by site, but the per-host reads and writes behind
    it were not, so an operator confined to one site could read another site's
    metrics — and approve, reboot or re-firewall its machines — by id.
    """
    from apps.hosts.models import Host

    host = Host.objects.filter(pk=host_id).first()
    if host is None or not _pkg.host_in_scope(user, host):
        return None
    return host


def host_or_404(request, host_id):
    """(host, None) when the caller may see it, else (None, a 404 Response).

    Reads as:

        host, denied = scoping.host_or_404(request, host_id)
        if denied:
            return denied
    """
    from rest_framework.response import Response

    host = visible_host(request.user, host_id)
    if host is None:
        return None, Response({"error": "Not found"}, status=404)
    return host, None
