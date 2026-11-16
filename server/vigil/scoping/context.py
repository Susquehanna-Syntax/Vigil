"""The request/job scope itself and the entry points that enter one (S1 phase
04a). ``Scope``, the fail-closed helpers a scoped manager uses when no scope is
set, and the context managers and decorators (``system``, ``as_user``,
``reading_as``, ``system_view``, ``system_job``) code says "cross site scope"
with. ``vigil.scoping`` re-exports everything here as before.
"""
import asyncio
import functools
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field

from ._state import _scope
from .sites import site_roles_for

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
    #: True for a browser request (S1 04g): only there does the user's header
    #: site selection narrow what they see. Jobs and as_user never set it.
    ui: bool = False
    #: Per-scope memo (a user's site roles), so a request that runs many
    #: scoped queries looks the roles up once. It dies with the scope.
    cache: dict = field(default_factory=dict, compare=False, repr=False)


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
    outside this module, as ``path:line in func``. (Since S5 07 the module is
    a package; "/scoping" skips every file of it.)"""
    import traceback
    for frame in reversed(traceback.extract_stack()[:-3]):
        name = frame.filename.replace("\\", "/")
        if ("/apps/" in name or "/apps_business/" in name or "/vigil/" in name) \
                and "/scoping" not in name:
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
    except Exception:  # noqa: BLE001,S110 — the alert must never break the caller
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
    return _entered(Scope("user", user=lambda: getattr(request, "user", None), ui=True))


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
    with _entered(Scope("user", user=scope.user, app=app, ui=scope.ui, cache=scope.cache)):
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
