"""Who may make the server sign a task (SEC-1).

A signed task is code that runs as root or SYSTEM on its host, limited only by
that agent's local allowlist. So every path that creates one must answer two
questions per target host, the same way the per-host endpoints already do
(``scoping.host_or_404`` + ``permissions.can``):

* may this user see the host at all? If not, it is reported as not found, so a
  scoped user cannot learn that a host exists in a site they cannot reach;
* may this user run tasks in the host's site (``tasks:run``)?

Writing a task definition is preparing to run one, so it needs ``tasks:run``
somewhere: a viewer can read the library but not add to it.
"""
from __future__ import annotations

from rest_framework.response import Response


def _can_run(user, host) -> bool:
    from apps.accounts.permissions import can
    from vigil import scoping

    return scoping.host_in_scope(user, host) and can(user, scoping.scope_of(host), "tasks", "run")


def runnable(user, hosts):
    """The hosts in *hosts* this user may run tasks on, order kept."""
    return [h for h in hosts if _can_run(user, h)]


def run_denied(user, hosts) -> Response | None:
    """None when *user* may run tasks on every host, else the response to send.

    Out-of-scope hosts answer 404 exactly like an unknown id; in-scope hosts the
    user lacks ``tasks:run`` on answer 403 and are named, since the user can
    already see them.
    """
    from vigil import scoping

    hidden = [h for h in hosts if not scoping.host_in_scope(user, h)]
    if hidden:
        return Response({"error": "One or more hosts were not found"}, status=404)
    refused = [h for h in hosts if not _can_run(user, h)]
    if refused:
        return Response({
            "error": "You may not run tasks on " + ", ".join(sorted(h.hostname for h in refused)),
        }, status=403)
    return None


def may_author(user) -> bool:
    """May *user* create or change task definitions? Needs tasks:run somewhere."""
    from apps.accounts.models import Role
    from apps.accounts.permissions import OWNER, can, role_of
    from vigil import scoping

    role = role_of(user)
    if role in (OWNER, Role.ADMIN):
        return True
    if role != Role.OPERATOR:
        return False
    site_ids = [sid for sid in scoping.site_roles_for(user) if sid != "__global__"]
    if not site_ids:
        # No per-site rows: a profile operator keeps the legacy operator powers.
        return can(user, None, "tasks", "run")
    return any(can(user, sid, "tasks", "run") for sid in site_ids)


def author_denied(user) -> Response | None:
    if may_author(user):
        return None
    return Response({"error": "Your role may not create or change tasks"}, status=403)


def fleet_runner_denied(user) -> Response | None:
    """For actions that span every site (rollouts): the caller must be allowed to
    run tasks fleet-wide, an admin or an operator whose authority is not limited
    to some sites. A viewer, or anyone scoped to particular sites, is refused."""
    from apps.accounts.models import Role
    from apps.accounts.permissions import OWNER, can, role_of
    from vigil import scoping

    rows = scoping.site_roles_for(user)
    if rows:
        glob = rows.get("__global__")
        if glob in (Role.ADMIN, OWNER):
            return None
        mods = scoping._sites_models()
        glob_site = mods.Site.objects.global_site() if mods is not None else None
        if glob == Role.OPERATOR and can(user, glob_site, "tasks", "run"):
            return None
        return Response({"error": "This acts on every site; it needs fleet-wide permission to run tasks"},
                        status=403)
    if role_of(user) in (OWNER, Role.ADMIN):
        return None
    if role_of(user) == Role.OPERATOR and can(user, None, "tasks", "run"):
        return None
    return Response({"error": "Your role may not run tasks"}, status=403)
