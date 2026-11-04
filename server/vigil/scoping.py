"""Site scope resolution — the single source of truth for "what is visible here".

Core (AGPL) never imports apps_business directly. Everything goes through this
module, which degrades to "no scoping" when the Business sites app is absent,
the same way apps/accounts/views.py:_branding() degrades.

Nothing in this module may sit in a path that could stop monitoring, alerting,
or agent ingest (SQSY-LICENSING §6).
"""
import asyncio
import contextvars
import functools
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.db import models as _dj_models
from django.db.models import Q

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
    mods = _sites_models()
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
    if site_id is None or _sites_models() is None:
        return qs
    return qs.filter(site_assignment__site_id=site_id)


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


def _reading_app():
    """The module the active view reads as (reading_as), or None."""
    scope = _scope.get()
    return scope.app if scope is not None else None


def global_site_id():
    mods = _sites_models()
    return _global_site_id(mods) if mods is not None else None


def grants_for(user) -> set:
    """The (app, verb) company-wide grants *user* holds (S1 04f). Memoised per
    user scope, like site roles."""
    mods = _sites_models()
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

    ids = visible_site_ids(user, app=app or _reading_app())
    if ids is None:
        return qs
    if not ids:
        return qs.none()

    mods = _sites_models()
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

    ids = visible_site_ids(user, app=app or _reading_app())
    if ids is None:
        return qs
    if not ids:
        return qs.none()

    mods = _sites_models()
    if mods is None:
        return qs

    glob = _global_site_id(mods)
    q = (Q(**{f"{field}__in": ids})
         | Q(**{f"{field}__isnull": True})
         | Q(**{f"{field}": glob}))
    return qs.filter(q)


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
    mods = _sites_models()
    if sid is None or mods is None:
        return None
    return None if sid == _global_site_id(mods) else sid


def _write_site(user, site_id):
    """The site to ask ``can()`` about for a write in *site_id* (None =
    global), or False when the write is refused outright."""
    mods = _sites_models()
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

    if _sites_models() is None:
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

    mods = _sites_models()
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

    mods = _sites_models()
    if mods is None:
        return {"enabled": False, "global": True, "sites": []}
    ids = visible_site_ids(user)
    qs = mods.Site.objects.filter(is_global=False).order_by("name")
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    sites = [{"id": str(site.pk), "name": site.name} for site in qs
             if _allowed(user, site.pk, app, verb)]
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

    mods = _sites_models()
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
    if host is None or not host_in_scope(user, host):
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


# ---------------------------------------------------------------------------
# Scoped by default (S1 phase 04a)
#
# Every request runs inside a *scope*: the user whose sites bound what the
# data layer returns. A model whose manager is a SiteScopedManager filters by
# that scope on every query, so a view that forgets filter_by_site still
# cannot read another site's rows. Code that must cross sites says so with
# ``Model.objects.unscoped(reason="...")`` or ``with scoping.system(reason=...)``,
# and vigil/test_scope_crossings.py pins every such call site.
#
# Outside a request (Celery, management commands, migrations, test bodies)
# there is no scope, and a scoped manager returns rows unfiltered: §6 forbids
# a scoping gap from stopping monitoring, alerting or agent ingest. Jobs whose
# output reaches a person (reports, exports, alert mail) enter
# ``as_user(...)`` explicitly; that is S1 phase 05.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What the data layer may return right now.

    ``kind`` is "user" (filter by ``user()``) or "system" (no filter, with a
    stated ``reason``). ``user`` is a callable so a request scope reads
    ``request.user`` at query time: DRF authenticates (API keys, tokens) inside
    the view, after middleware has run, and writes the result back onto the
    Django request.
    """
    kind: str
    user: object = None
    reason: str = ""
    #: The module a view reads as (S1 04f): a company-wide grant on it widens
    #: that view's scoped queries, e.g. the inventory list reading hosts.
    app: str | None = None
    #: Per-scope memo (a user's site roles), so a request that runs many
    #: scoped queries looks the roles up once. It dies with the scope.
    cache: dict = field(default_factory=dict, compare=False, repr=False)


_scope: contextvars.ContextVar = contextvars.ContextVar("vigil_site_scope", default=None)


def current_scope():
    """The active Scope, or None outside any request or ``as_user`` block."""
    return _scope.get()


# ── No scope fails closed (decisions-06 item 17) ──────────────────────────────
# A scoped query with no scope set is a path nobody declared: a background job,
# a thread, a startup hook. It gets nothing back rather than every site's rows.
# In strict mode (the test runner) it raises, so CI names the path; in
# production it logs, and leaves a record the instance admins' banner shows.

class ScopeMissing(RuntimeError):
    """A site-scoped query ran with no scope."""


SCOPE_ALERT_KEY = "vigil:scope-missing"
_log = logging.getLogger("vigil.scoping")


def _caller():
    """Where the unscoped query came from: the first frame in Vigil code
    outside this module, as ``path:line in func``."""
    import traceback
    for frame in reversed(traceback.extract_stack()[:-3]):
        name = frame.filename.replace("\\", "/")
        if ("/apps/" in name or "/apps_business/" in name or "/vigil/" in name) \
                and not name.endswith("vigil/scoping.py"):
            short = name.split("/server/")[-1].split("/Vigil_Business/")[-1]
            return f"{short}:{frame.lineno} in {frame.name}"
    return "unknown"


def _no_scope(qs):
    from django.conf import settings
    where = _caller()
    message = f"{qs.model.__name__} was queried with no site scope ({where})"
    if getattr(settings, "VIGIL_SCOPE_STRICT", False):
        raise ScopeMissing(message + ": wrap the entry point in scoping.system(reason=...) "
                                     "or scoping.as_user(...)")
    _log.error("%s; it got no rows back", message)
    try:
        from django.core.cache import cache
        from django.utils.timezone import now
        cache.set(SCOPE_ALERT_KEY, {"model": qs.model.__name__, "where": where,
                                    "at": now().isoformat()}, 24 * 3600)
    except Exception:  # noqa: BLE001 — the alert must never break the caller
        pass
    return qs.none()


def scope_alert():
    """The last unscoped-query record, or None."""
    try:
        from django.core.cache import cache
        return cache.get(SCOPE_ALERT_KEY)
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def no_scope_for_tests():
    """Clear the scope inside a test, to exercise what an undeclared path gets."""
    token = _scope.set(None)
    try:
        yield
    finally:
        _scope.reset(token)


def is_instance_admin(user) -> bool:
    """An administrator of the whole install (mirrors the Business sites
    app): a superuser, an admin profile with no site rows, or a Global admin
    row. A per-site row never counts, and a site-scoped API key never does."""
    from apps.accounts.models import Role
    from apps.accounts.permissions import OWNER, _unscoped_role

    if not (user and user.is_authenticated):
        return False
    if getattr(user, "key_site_ids", None) is not None:
        return False
    if user.is_superuser:
        return True
    rows = site_roles_for(user)
    if not rows:
        return _unscoped_role(user) in (OWNER, Role.ADMIN)
    return rows.get("__global__") in (OWNER, Role.ADMIN)


@contextmanager
def _entered(scope):
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def request_scope(request):
    """The scope for one HTTP request: whoever ``request.user`` is when the
    query runs. An anonymous request sees no site-owned rows at all."""
    return _entered(Scope("user", user=lambda: getattr(request, "user", None)))


def as_user(user):
    """Run the block as `user`: scoped managers return what they may see."""
    return _entered(Scope("user", user=lambda: user))


@contextmanager
def reading_as(app):
    """Inside a user scope, read as module *app*: a company-wide grant on it
    widens every scoped query in the block (S1 04f). Elsewhere a no-op."""
    scope = _scope.get()
    if scope is None or scope.kind != "user":
        yield
        return
    with _entered(Scope("user", user=scope.user, app=app, cache=scope.cache)):
        yield


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def module_view(app):
    """Decorate a list view that belongs to module *app* (see reading_as).
    Reads only: a grant is never a way to write, so a POST to the same view
    keeps the user's own sites for every lookup it makes."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(request, *args, **kwargs):
            if getattr(request, "method", "GET") not in _SAFE_METHODS:
                return fn(request, *args, **kwargs)
            with reading_as(app):
                return fn(request, *args, **kwargs)
        wrapper._vigil_module = app
        return wrapper
    return deco


def _require_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("crossing site scope needs a non-empty reason")


def system(*, reason):
    """Run the block with no site filter, for work no person is behind
    (agent check-ins, maintenance). `reason` is required; every call site is
    pinned by vigil/test_scope_crossings.py."""
    _require_reason(reason)
    return _entered(Scope("system", reason=reason))


def system_view(*, reason):
    """Decorate a view that no person is behind (an agent authenticated by
    its host token, a public status page whose token is the access control).
    The whole view runs in a system scope. Put it outermost, above
    ``@api_view``, so the URL guard test can see ``_vigil_scope_reason`` on
    the callback. Works on async views too."""
    _require_reason(reason)

    def deco(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                with system(reason=reason):
                    return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with system(reason=reason):
                    return fn(*args, **kwargs)
        wrapper._vigil_scope_reason = reason
        return wrapper

    return deco


def system_job(*, reason):
    """Decorate a background job (a Celery task body, a thread target) that
    no person is behind: it runs in a system scope. Put it under
    ``@shared_task`` so the task's ``run`` carries ``_vigil_scope_reason``;
    vigil/test_scope_jobs.py fails for any task without one."""
    _require_reason(reason)

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with system(reason=reason):
                return fn(*args, **kwargs)
        wrapper._vigil_scope_reason = reason
        return wrapper

    return deco


def install_command_scope():
    """Management commands run in a system scope: whoever runs one has the
    shell, and with it every row already. Called once from an AppConfig."""
    from django.core.management.base import BaseCommand

    if getattr(BaseCommand.execute, "_vigil_scoped", False):
        return
    plain = BaseCommand.execute

    @functools.wraps(plain)
    def execute(self, *args, **options):
        with system(reason="management command: run by an operator at the shell"):
            return plain(self, *args, **options)

    execute._vigil_scoped = True
    BaseCommand.execute = execute


def scoped_user():
    """The user the active scope filters by, or None when nothing filters
    (no scope, or a system scope)."""
    scope = _scope.get()
    if scope is None or scope.kind != "user":
        return None
    return scope.user()


class SiteScopedManager(_dj_models.Manager):
    """A default manager that narrows every query to the active scope.

    ``path``/``cascade_global`` mean what they mean in filter_by_site;
    ``field`` switches to filter_by_site_field for models with a plain
    site-id column. Forward FK reads (``alert.host``) go through the model's
    base manager, which Django keeps as a plain Manager, so they are never
    filtered; only queries started from this manager are.
    """

    def __init__(self, *, path="", cascade_global=False, field=None, app=None):
        super().__init__()
        self.site_path = path
        self.site_cascade_global = cascade_global
        self.site_field = field
        #: The module these rows belong to: a company-wide grant on it (S1
        #: 04f) widens reads. A view reading as another module overrides it.
        self.site_app = app

    def deconstruct(self):
        # Managers are not used in migrations (use_in_migrations is False),
        # but deconstruct must still describe the constructor arguments.
        ret = list(super().deconstruct())
        ret[3] = {"path": self.site_path, "cascade_global": self.site_cascade_global,
                  "field": self.site_field, "app": self.site_app}
        return tuple(ret)

    def _config(self):
        """The manager declared on the model. Django builds related managers
        (``run.tasks``, prefetches) by subclassing the default manager's class
        and calling it with no arguments, which would reset path/field to
        their defaults; the declared instance keeps the real ones."""
        declared = getattr(self.model, "_default_manager", None)
        return declared if isinstance(declared, SiteScopedManager) else self

    def get_queryset(self):
        qs = super().get_queryset()
        scope = _scope.get()
        if scope is None:
            # Community installs have no sites, so there is no boundary to
            # hold and nothing to fail closed for.
            return qs if _sites_models() is None else _no_scope(qs)
        if scope.kind == "system":
            return qs
        user = scope.user()
        if user is None or not getattr(user, "is_authenticated", False):
            return qs.none()
        cfg = self._config()
        app = scope.app or cfg.site_app
        if cfg.site_field:
            return filter_by_site_field(qs, user, field=cfg.site_field, app=app)
        return filter_by_site(qs, user, path=cfg.site_path,
                              cascade_global=cfg.site_cascade_global, app=app)

    def unscoped(self, *, reason):
        """Every row, whatever the scope. Say why: the reason is pinned with
        the call site in vigil/test_scope_crossings.py.

        On a related manager (``run.tasks.unscoped(...)``) the relation's own
        filter still applies: only the site filter is lifted."""
        _require_reason(reason)
        token = _scope.set(Scope("system", reason=reason))
        try:
            return self.get_queryset()
        finally:
            _scope.reset(token)


class SiteScopeMiddleware:
    """Enter a request scope for the whole request, including the body of a
    streaming response, which Django iterates after this middleware returns."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with request_scope(request):
            response = self.get_response(request)
        if getattr(response, "streaming", False):
            response.streaming_content = _rescoped(request, response.streaming_content)
        return response


def _rescoped(request, chunks):
    it = iter(chunks)
    while True:
        with request_scope(request):
            try:
                chunk = next(it)
            except StopIteration:
                return
        yield chunk
