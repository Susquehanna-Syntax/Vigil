"""Scoped managers and the request middleware (S1 phase 04a). ``SiteScopedManager``
narrows every query it starts to the active scope, and ``SiteScopeMiddleware``
opens a scope for the whole request. ``vigil.scoping`` re-exports everything
here as before.
"""
from django.db import models as _dj_models

from vigil import scoping as _pkg

from ._state import _scope
from .context import Scope, _no_scope, _require_reason, request_scope
from .sites import filter_by_site_field


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
        #: Whether a company-wide grant (S1 04f) may widen these rows at all.
        #: It never widens by itself: only a read-only module_view, which
        #: names the module it reads as, widens, and only models that carry
        #: an app here. Rows holding secrets (one-time tickets, sessions)
        #: carry none, so no grant ever reaches another site's.
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
            return qs if _pkg._sites_models() is None else _no_scope(qs)
        if scope.kind == "system":
            return qs
        user = scope.user()
        if user is None or not getattr(user, "is_authenticated", False):
            return qs.none()
        cfg = self._config()
        # scope.app is set only inside module_view on a safe method.
        # False, not None: None would let the filter fall back to the reading app.
        app = scope.app if (scope.app and cfg.site_app) else False
        if cfg.site_field:
            return filter_by_site_field(qs, user, field=cfg.site_field, app=app)
        # _pkg. so a test that patches vigil.scoping.filter_by_site reaches this.
        return _pkg.filter_by_site(qs, user, path=cfg.site_path,
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
