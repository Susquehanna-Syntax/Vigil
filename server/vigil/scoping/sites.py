"""Site membership, visibility and role lookups (S1 phase 04).

What resources a site can see, which sites a user can see, the roles and
company-wide grants behind them, and the read-side narrowing every scoped
query goes through. ``vigil.scoping`` re-exports everything here as before.
"""
import logging

from django.db.models import Q

from vigil import scoping as _pkg

from ._state import _scope

logger = logging.getLogger("vigil.scoping")

#: Core model -> (assignment model name, FK field name on the assignment).
#: Every assignment model uses the reverse accessor ``site_assignment``, which
#: is what lets the queries below stay uniform.
_ASSIGNMENTS = {
    # Without this entry scope_of(host) was always None, so every per-host
    # capability check (edit, update_agent, firewall, reprovision, tasks:run)
    # asked about "no site" and got the user's strongest role anywhere: a
    # per-site operator restriction never applied to hosts (SEC-1).
    "hosts.Host": ("HostSiteAssignment", "host"),
    "baselines.Playbook": ("PlaybookSiteAssignment", "playbook"),
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
    mods = _pkg._sites_models()
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
    mods = _pkg._sites_models()
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
    mods = _pkg._sites_models()
    if assignment is None or mods is None:
        return None
    row = assignment.objects.filter(**{field: obj}).select_related("site").first()
    if row is not None:
        return row.site
    return mods.Site.objects.global_site()


_UNSET = object()


def site_id_of(obj):
    """The id of the site `obj` belongs to, or None when it is global (no
    assignment, or assigned to the global site). Cached on the instance."""
    cached = getattr(obj, "_vigil_site_id", _UNSET)
    if cached is not _UNSET:
        return cached
    site = scope_of(obj)
    sid = None if site is None or site.is_global else site.id
    obj._vigil_site_id = sid
    return sid


def within_site(resource, host) -> bool:
    """May `resource` (a playbook or automation) act on `host`?

    A global resource reaches every host. A site's resource reaches only that
    site's hosts: a Lab automation targeting "all hosts" means all of Lab's,
    never West's. Without this, a site admin's playbook or automation ran on
    every site's machines."""
    sid = site_id_of(resource)
    if sid is None:
        return True
    return site_id_of(host) == sid


def prime_site_ids(hosts):
    """Set every host's cached site id in one query, so within_site() over a
    fleet costs one query rather than one per host."""
    hosts = list(hosts)
    mods = _pkg._sites_models()
    if mods is None:
        for h in hosts:
            h._vigil_site_id = None
        return hosts
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    rows = dict(mods.HostSiteAssignment.objects.filter(
        host_id__in=[h.pk for h in hosts]).values_list("host_id", "site_id"))
    for h in hosts:
        sid = rows.get(h.pk)
        h._vigil_site_id = None if sid is None or sid == glob else sid
    return hosts


def hosts_in_site(qs, site_id):
    """`qs` (of Host) narrowed to one site's hosts; untouched for None (a
    global resource reaches every host)."""
    if site_id is None or _pkg._sites_models() is None:
        return qs
    return qs.filter(site_assignment__site_id=site_id)


def suppress(site, obj):
    """Hide a global resource from one site. Idempotent."""
    mods = _pkg._sites_models()
    if mods is None:
        return
    from django.contrib.contenttypes.models import ContentType
    mods.GlobalSuppression.objects.get_or_create(
        site=site, content_type=ContentType.objects.get_for_model(type(obj)),
        object_id=obj.id)


def unsuppress(site, obj):
    """Undo suppress(). Idempotent."""
    mods = _pkg._sites_models()
    if mods is None:
        return
    from django.contrib.contenttypes.models import ContentType
    mods.GlobalSuppression.objects.filter(
        site=site, content_type=ContentType.objects.get_for_model(type(obj)),
        object_id=obj.id).delete()


def execution_allowed(obj) -> bool:
    """May this resource's action run right now?

    Site-scoped automations and playbooks pause under a lapsed license; global
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
    mods = _pkg._sites_models()
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
    mods = _pkg._sites_models()
    if mods is None or not getattr(user, "pk", None):
        return {}

    scope = _scope.get()
    key = ("site_roles", user.pk)
    if scope is not None and scope.kind == "user" and key in scope.cache:
        return dict(scope.cache[key])

    out = {}
    rows = (mods.UserSiteRole.objects
            .filter(user=user).select_related("site"))
    for row in rows:
        out[row.site_id] = row.role
        if row.site.is_global:
            out["__global__"] = row.role
    if scope is not None and scope.kind == "user":
        scope.cache[key] = dict(out)
    return out


def viewing_site_ids(user, app=None):
    """The site ids a scoped *read* shows (filter_by_site and friends): what
    *user* may see (visible_site_ids), narrowed to the sites they ticked in
    the header switcher (S1 04g). Authority checks keep visible_site_ids, so
    ticking one site never takes away what an instance admin may do; it only
    changes what the pages list. The selection can only narrow, and when it
    and the visible sites share nothing it is ignored."""
    from apps.accounts.permissions import visible_site_ids

    ids = visible_site_ids(user, app=app)
    if ids == set():
        return ids
    chosen = site_selection_for(user)
    if not chosen:
        return ids
    return (chosen if ids is None else (ids & chosen)) or ids


def site_selection_for(user):
    """The sites *user* ticked in the header switcher (S1 04g), as a set of
    existing site ids, or None when the selection does not apply: outside a
    browser request, for a site-scoped API key, on Community, or when nothing
    is ticked. Ids of deleted sites drop out, so a stale selection never
    narrows to nothing."""
    scope = _scope.get()
    if scope is None or scope.kind != "user" or not scope.ui:
        return None
    if getattr(user, "key_site_ids", None) is not None or not getattr(user, "pk", None):
        return None
    mods = _pkg._sites_models()
    if mods is None:
        return None
    key = ("site_selection", user.pk)
    if key not in scope.cache:
        # user.profile is usually loaded already (role checks read it), so
        # this costs no query on most requests.
        try:
            raw = user.profile.site_selection
        except Exception:  # noqa: BLE001 — no profile row (RelatedObjectDoesNotExist)
            raw = []
        wanted = _as_uuids(raw)
        scope.cache[key] = (set(mods.Site.objects.filter(pk__in=wanted).values_list("pk", flat=True))
                            if wanted else set())
    return set(scope.cache[key]) or None


def _as_uuids(raw):
    import uuid
    out = set()
    for value in raw if isinstance(raw, list) else []:
        try:
            out.add(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


def _reading_app():
    """The module the active view reads as (reading_as), or None."""
    scope = _scope.get()
    return scope.app if scope is not None else None


def global_site_id():
    mods = _pkg._sites_models()
    return _global_site_id(mods) if mods is not None else None


def grants_for(user) -> set:
    """The (app, verb) company-wide grants *user* holds (S1 04f). Memoised per
    user scope, like site roles."""
    mods = _pkg._sites_models()
    if mods is None or not getattr(user, "pk", None) or not hasattr(mods, "ModuleGrant"):
        return set()
    scope = _scope.get()
    key = ("grants", user.pk)
    if scope is not None and scope.kind == "user" and key in scope.cache:
        return scope.cache[key]
    out = set(mods.ModuleGrant.objects.filter(user=user).values_list("app", "verb"))
    if scope is not None and scope.kind == "user":
        scope.cache[key] = out
    return out


def capabilities_for(user, site):
    """The set of (app, verb) pairs granted to `user` in `site`, or None when
    no UserSiteRole row backs them there.

    None and set() mean different things: None is "no row, fall back to the
    legacy operator powers", while an empty set is "a row exists and grants
    nothing", which denies.
    """
    mods = _pkg._sites_models()
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


def _global_site_id(mods):
    """The global site's id, looked up once per scope."""
    scope = _scope.get()
    if scope is not None and scope.kind == "user" and "global_site_id" in scope.cache:
        return scope.cache["global_site_id"]
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    if scope is not None and scope.kind == "user":
        scope.cache["global_site_id"] = glob
    return glob


def filter_by_site(qs, user, path="", cascade_global=False, app=None):
    """Narrow `qs` to what `user` may see, by site.

    `path` is the ORM path from the queryset's model to whatever carries the
    site assignment: "" when the model itself does (Host, Playbook,
    Automation), "host__" when it hangs off a host (Alert).

    `cascade_global` distinguishes the two meanings of "global":

    * **False** (hosts, alerts) — unassigned means *membership* of the global
      site, so only someone who can see Global sees those rows.
    * **True** (playbooks, automations, channels) — global means *applies
      everywhere*, so anyone with access to any site must see it. A Lab-only
      admin has to see the global playbook that governs Lab.

    Unscoped users get the queryset back untouched, which is what keeps
    existing installs behaving exactly as before.
    """
    from apps.accounts.permissions import visible_site_ids

    ids = viewing_site_ids(user, app=_reading_app() if app is None else (app or None))
    if ids is None:
        return qs
    if not ids:
        return qs.none()

    mods = _pkg._sites_models()
    if mods is None:
        return qs

    q = Q(**{f"{path}site_assignment__site_id__in": ids})
    if cascade_global:
        q |= Q(**{f"{path}site_assignment__isnull": True})
        q |= Q(**{f"{path}site_assignment__site__is_global": True})
    else:
        glob = _global_site_id(mods)
        if glob in ids:
            q |= Q(**{f"{path}site_assignment__isnull": True})
    # No DISTINCT: every path is forward foreign keys ending at a one-to-one
    # site assignment, so the join never repeats a row. DISTINCT would change
    # meaning now the filter applies by default: values_list() without an
    # ordering would collapse equal values.
    return qs.filter(q)


def filter_by_site_field(qs, user, field="site_id", app=None):
    """Narrow `qs` by a plain site-id column (UpdatePolicy, StatusPage).

    Null in `field` means "no site restriction": the row applies everywhere,
    so every user with access to any site sees it, the same as
    filter_by_site(..., cascade_global=True). A row whose `field` is the
    global site's id counts as null. Unscoped users get `qs` untouched.
    """
    from apps.accounts.permissions import visible_site_ids

    ids = viewing_site_ids(user, app=_reading_app() if app is None else (app or None))
    if ids is None:
        return qs
    if not ids:
        return qs.none()

    mods = _pkg._sites_models()
    if mods is None:
        return qs

    glob = _global_site_id(mods)
    q = (Q(**{f"{field}__in": ids})
         | Q(**{f"{field}__isnull": True})
         | Q(**{f"{field}": glob}))
    return qs.filter(q)
