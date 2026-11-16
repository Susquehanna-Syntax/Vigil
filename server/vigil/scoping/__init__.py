"""Site scope resolution — the single source of truth for "what is visible here".

Core (AGPL) never imports apps_business directly. Everything goes through this
module, which degrades to "no scoping" when the Business sites app is absent,
the same way apps/accounts/views.py:_branding() degrades.

Nothing in this module may sit in a path that could stop monitoring, alerting,
or agent ingest (SQSY-LICENSING §6).
"""
from ._state import _scope  # noqa: F401
from .context import (  # noqa: F401
    _SAFE_METHODS,
    SCOPE_ALERT_KEY,
    Scope,
    ScopeMissing,
    _caller,
    _entered,
    _log,
    _no_scope,
    _require_reason,
    as_user,
    current_scope,
    install_command_scope,
    is_instance_admin,
    module_view,
    no_scope_for_tests,
    reading_as,
    request_scope,
    scope_alert,
    scoped_user,
    system,
    system_job,
    system_view,
)
from .managers import (  # noqa: F401
    SiteScopedManager,
    SiteScopeMiddleware,
    _rescoped,
)
from .sites import (  # noqa: F401
    _ASSIGNMENTS,
    _UNSET,
    _as_uuids,
    _assignment_for,
    _global_site_id,
    _reading_app,
    _sites_models,
    _suppressed_ids,
    capabilities_for,
    execution_allowed,
    filter_by_site,
    filter_by_site_field,
    global_site_id,
    grants_for,
    hosts_in_site,
    logger,
    prime_site_ids,
    resources_for,
    scope_of,
    site_id_of,
    site_roles_for,
    site_selection_for,
    sites_for_hosts,
    suppress,
    unsuppress,
    viewing_site_ids,
    within_site,
)
from .writes import (  # noqa: F401
    _allowed,
    _write_site,
    host_in_scope,
    host_or_404,
    owning_site_id,
    place,
    row_site_id,
    site_for_write,
    visible_host,
    with_sites,
    writable_sites,
    write_denied,
)
