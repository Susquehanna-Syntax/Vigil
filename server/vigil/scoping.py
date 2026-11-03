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
    if scope is not None and key in scope.cache:
        return dict(scope.cache[key])

    out = {}
    rows = (mods.UserSiteRole.objects
            .filter(user=user).select_related("site"))
    for row in rows:
        out[row.site_id] = row.role
        if row.site.is_global:
            out["__global__"] = row.role
    if scope is not None:
        scope.cache[key] = dict(out)
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
    if scope is not None and "global_site_id" in scope.cache:
        return scope.cache["global_site_id"]
    glob = mods.Site.objects.filter(is_global=True).values_list("id", flat=True).first()
    if scope is not None:
        scope.cache["global_site_id"] = glob
    return glob


def filter_by_site(qs, user, path="", cascade_global=False):
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

    ids = visible_site_ids(user)
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
    return qs.filter(q).distinct()


def filter_by_site_field(qs, user, field="site_id"):
    """Narrow `qs` by a plain site-id column (UpdatePolicy, StatusPage).

    Null in `field` means "no site restriction": the row applies everywhere,
    so every user with access to any site sees it, the same as
    filter_by_site(..., cascade_global=True). A row whose `field` is the
    global site's id counts as null. Unscoped users get `qs` untouched.
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

    glob = _global_site_id(mods)
    q = (Q(**{f"{field}__in": ids})
         | Q(**{f"{field}__isnull": True})
         | Q(**{f"{field}": glob}))
    return qs.filter(q)


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
    #: Per-scope memo (a user's site roles), so a request that runs many
    #: scoped queries looks the roles up once. It dies with the scope.
    cache: dict = field(default_factory=dict, compare=False, repr=False)


_scope: contextvars.ContextVar = contextvars.ContextVar("vigil_site_scope", default=None)


def current_scope():
    """The active Scope, or None outside any request or ``as_user`` block."""
    return _scope.get()


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

    def __init__(self, *, path="", cascade_global=False, field=None):
        super().__init__()
        self.site_path = path
        self.site_cascade_global = cascade_global
        self.site_field = field

    def deconstruct(self):
        # Managers are not used in migrations (use_in_migrations is False),
        # but deconstruct must still describe the constructor arguments.
        ret = list(super().deconstruct())
        ret[3] = {"path": self.site_path, "cascade_global": self.site_cascade_global,
                  "field": self.site_field}
        return tuple(ret)

    def get_queryset(self):
        qs = super().get_queryset()
        scope = _scope.get()
        if scope is None or scope.kind == "system":
            return qs
        user = scope.user()
        if user is None or not getattr(user, "is_authenticated", False):
            return qs.none()
        if self.site_field:
            return filter_by_site_field(qs, user, field=self.site_field)
        return filter_by_site(qs, user, path=self.site_path,
                              cascade_global=self.site_cascade_global)

    def unscoped(self, *, reason):
        """Every row, whatever the scope. Say why: the reason is pinned with
        the call site in vigil/test_scope_crossings.py."""
        _require_reason(reason)
        return super().get_queryset()


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
