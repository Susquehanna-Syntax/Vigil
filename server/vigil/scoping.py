"""Site scope resolution — the single source of truth for "what is visible here".

Core (AGPL) never imports apps_business directly. Everything goes through this
module, which degrades to "no scoping" when the Business sites app is absent,
the same way apps/accounts/views.py:_branding() degrades.

Nothing in this module may sit in a path that could stop monitoring, alerting,
or agent ingest (SQSY-LICENSING §6).
"""
import logging

from django.db.models import Q

logger = logging.getLogger("vigil.scoping")

#: Core model -> (assignment model name, FK field name on the assignment).
#: Every assignment model uses the reverse accessor ``site_assignment``, which
#: is what lets the queries below stay uniform.
_ASSIGNMENTS = {
    "baselines.Baseline": ("BaselineSiteAssignment", "baseline"),
    "automations.Automation": ("AutomationSiteAssignment", "automation"),
    "alerts.NotificationChannel": ("ChannelSiteAssignment", "channel"),
}


def _sites_models():
    """The Business sites models module, or None when absent.

    Two distinct absences, both of which must degrade rather than raise:
    the app is not in INSTALLED_APPS (importing its models would raise
    RuntimeError about a missing app_label), or the commercial directory has
    been removed outright (ImportError).
    """
    from django.apps import apps as django_apps
    if not django_apps.is_installed("apps_business.sites"):
        return None
    try:
        from apps_business.sites import models
        return models
    except ImportError:
        return None


def _assignment_for(model):
    label = f"{model._meta.app_label}.{model.__name__}"
    entry = _ASSIGNMENTS.get(label)
    mods = _sites_models()
    if entry is None or mods is None:
        return None, None
    name, field = entry
    return getattr(mods, name), field


def resources_for(model, site):
    """Resources of `model` visible in `site`: its own, plus global ones it
    has not suppressed. Unassigned resources count as global, which is why an
    install that has never used sites resolves exactly as it did before."""
    assignment, _field = _assignment_for(model)
    if assignment is None or site is None:
        return model.objects.all()

    own = Q(site_assignment__site=site)
    # "Global" is either an explicit assignment to the global site or no
    # assignment at all — the two are deliberately equivalent.
    is_global = Q(site_assignment__isnull=True) | Q(site_assignment__site__is_global=True)

    suppressed = _suppressed_ids(site, model)
    if suppressed:
        is_global &= ~Q(id__in=suppressed)

    return model.objects.filter(own | is_global).distinct()


def _suppressed_ids(site, model):
    mods = _sites_models()
    if mods is None:
        return set()
    from django.contrib.contenttypes.models import ContentType
    ct = ContentType.objects.get_for_model(model)
    return set(
        mods.GlobalSuppression.objects
        .filter(site=site, content_type=ct)
        .values_list("object_id", flat=True))


def scope_of(obj):
    """The Site owning `obj`, or the global site when it has no assignment."""
    assignment, field = _assignment_for(type(obj))
    mods = _sites_models()
    if assignment is None or mods is None:
        return None
    row = assignment.objects.filter(**{field: obj}).select_related("site").first()
    if row is not None:
        return row.site
    return mods.Site.objects.global_site()


def suppress(site, obj):
    """Hide a global resource from one site. Idempotent."""
    mods = _sites_models()
    if mods is None:
        return
    from django.contrib.contenttypes.models import ContentType
    mods.GlobalSuppression.objects.get_or_create(
        site=site, content_type=ContentType.objects.get_for_model(type(obj)),
        object_id=obj.id)


def unsuppress(site, obj):
    """Undo suppress(). Idempotent."""
    mods = _sites_models()
    if mods is None:
        return
    from django.contrib.contenttypes.models import ContentType
    mods.GlobalSuppression.objects.filter(
        site=site, content_type=ContentType.objects.get_for_model(type(obj)),
        object_id=obj.id).delete()


def execution_allowed(obj) -> bool:
    """May this resource's action run right now?

    Site-scoped automations and baselines pause under a lapsed license; global
    ones never do. NotificationChannel is never gated here — alert delivery is
    not a licensable behavior (§6).
    """
    from apps.alerts.models import NotificationChannel
    if isinstance(obj, NotificationChannel):
        return True

    from vigil import licensing
    if licensing.has_feature("sites"):
        return True

    site = scope_of(obj)
    return site is None or site.is_global


def sites_for_hosts(host_ids):
    """Map host id -> Site for `host_ids`. Unassigned hosts map to the global
    site, because unassigned *is* membership of Global. Returns {} when the
    Business sites app is absent.

    Two queries regardless of how many hosts are passed.
    """
    mods = _sites_models()
    host_ids = list(host_ids)
    if mods is None or not host_ids:
        return {}

    assigned = {
        row.host_id: row.site
        for row in (mods.HostSiteAssignment.objects
                    .filter(host_id__in=host_ids).select_related("site"))
    }
    if len(assigned) == len(host_ids):
        return assigned

    glob = mods.Site.objects.global_site()
    return {hid: assigned.get(hid, glob) for hid in host_ids}


def site_roles_for(user):
    """Map site id -> role string for `user`, plus ``"__global__"`` for their
    row on the global site. Empty when the Business sites app is absent or
    the user holds no rows — which is what makes per-site roles opt-in.
    """
    mods = _sites_models()
    if mods is None or not getattr(user, "pk", None):
        return {}

    out = {}
    rows = (mods.UserSiteRole.objects
            .filter(user=user).select_related("site"))
    for row in rows:
        out[row.site_id] = row.role
        if row.site.is_global:
            out["__global__"] = row.role
    return out


def capabilities_for(user, site):
    """The set of (app, verb) pairs granted to `user` in `site`, or None when
    no UserSiteRole row backs them there.

    None and set() mean different things: None is "no row, fall back to the
    legacy operator powers", while an empty set is "a row exists and grants
    nothing", which denies.
    """
    mods = _sites_models()
    if mods is None or site is None or not getattr(user, "pk", None):
        return None

    row = (mods.UserSiteRole.objects
           .filter(user=user, site=site)
           .prefetch_related("capabilities").first())
    if row is None:
        # Fall back to their global row, which is the floor.
        row = (mods.UserSiteRole.objects
               .filter(user=user, site__is_global=True)
               .prefetch_related("capabilities").first())
    if row is None:
        return None
    return {(c.app, c.verb) for c in row.capabilities.all()}


def filter_by_site(qs, user, path=""):
    """Narrow `qs` to what `user` may see, by site.

    `path` is the ORM path from the queryset's model to whatever carries the
    site assignment: "" when the model itself does (Host, Baseline,
    Automation), "host__" when it hangs off a host (Alert).

    An unassigned object belongs to Global, so it is visible to anyone who
    can see Global. Unscoped users get the queryset back untouched, which is
    what keeps existing installs behaving exactly as before.
    """
    from apps.accounts.permissions import visible_site_ids

    ids = visible_site_ids(user)
    if ids is None:
        return qs
    if not ids:
        return qs.none()

    mods = _sites_models()
    if mods is None:
        return qs

    q = Q(**{f"{path}site_assignment__site_id__in": ids})
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    if glob in ids:
        q |= Q(**{f"{path}site_assignment__isnull": True})
    return qs.filter(q).distinct()


def host_in_scope(user, host) -> bool:
    """May `user` reach this host at all? False means the caller should 404
    rather than 403 — a scoped user should not learn that a host exists in a
    site they cannot see."""
    from apps.accounts.permissions import visible_site_ids

    ids = visible_site_ids(user)
    if ids is None:
        return True
    if not ids:
        return False

    mods = _sites_models()
    if mods is None:
        return True

    row = mods.HostSiteAssignment.objects.filter(host=host).first()
    if row is not None:
        return row.site_id in ids
    # Unassigned means Global.
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    return glob in ids
