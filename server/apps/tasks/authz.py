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


# ── Reads ─────────────────────────────────────────────────────────────────────
# A task's output is whatever its host printed, so history and run detail are
# scoped by site like the hosts they ran on (SEC-1). A run is visible when any
# of its tasks is (decisions-06 item 5); a run that also touched hosts the
# caller cannot see is *trimmed*: its tasks are only the caller's (the Task
# manager narrows run.tasks), and its host count and state are recomputed
# from those tasks, so nothing about the other site's hosts shows through.

def visible_tasks(qs, user):
    """Tasks on hosts *user* may see."""
    from vigil import scoping
    return scoping.filter_by_site(qs, user, path="host__")


def visible_runs(qs, user):
    """Runs with at least one task on a host *user* may see."""
    from apps.accounts.permissions import visible_site_ids

    from .models import Task

    if visible_site_ids(user) is None:
        return qs
    return qs.filter(pk__in=visible_tasks(Task.objects.all(), user).values("run_id"))


def run_visible(user, run) -> bool:
    from .models import TaskRun
    return visible_runs(TaskRun.objects.filter(pk=run.pk), user).exists()


def trim_run(run, user):
    """Cut *run* down to what *user* may see, in memory, and return it.

    A run fully in view comes back untouched. One that also touched hosts the
    caller cannot see gets ``host_count`` and ``state`` recomputed from the
    caller's own tasks and is marked trimmed: ``TaskRun.save`` refuses it, so a
    view can never write the cut-down numbers back.
    """
    from apps.accounts.permissions import visible_site_ids

    from django.db.models import Max

    from .models import Task
    from .summary import run_outcome

    if visible_site_ids(user) is None:
        return run
    every = Task.objects.unscoped(
        reason="run trimming: a run is trimmed when any of its tasks is on a site the caller cannot see"
    ).filter(run=run)
    seen = visible_tasks(every, user)
    if not every.exclude(pk__in=seen.values("pk")).exists():
        return run
    run.host_count = seen.values("host_id").distinct().count()
    if run.host_count:
        # With none of its tasks in view the run is not visible at all
        # (visible_runs), so there is nothing to recompute.
        run.state = run_outcome(seen.values_list("state", "on_failure"))
        # When the run as a whole finished says when *other* sites' hosts did,
        # or that they are still going: the caller's part finished when its
        # last task did. step_count and flow_snapshot describe the definition,
        # the same for every host, so they stay.
        run.finished_at = (None if run.state == run.State.RUNNING
                           else seen.aggregate(last=Max("completed_at"))["last"])
    run._trimmed = True
    return run


def trim_runs(runs, user):
    return [trim_run(run, user) for run in runs]


# ── Rollouts ─────────────────────────────────────────────────────────────────
# A rollout started for a site (an automation or update policy of that site)
# carries its ``site_id`` and only ever touches that site's hosts. One without
# a site is fleet-wide: it was started by a fleet-wide runner, or by a global
# automation or policy. decisions-06 item 5: visible when any of its hosts is,
# trimmed to them (wave hosts and counts read tasks through the scoped Task
# manager, and trim_run cuts each run). Item 1: only fleet-wide runners act on
# a fleet-wide rollout; a site's rollout is acted on by who may run tasks there.

def _visible_host_ids(user) -> set[str]:
    from apps.hosts.models import Host
    from vigil import scoping
    return {str(pk) for pk in scoping.filter_by_site(Host.objects.all(), user).values_list("id", flat=True)}


def visible_rollouts(rollouts, user) -> list:
    """The rollouts in *rollouts* that *user* may see, order kept."""
    from apps.accounts.permissions import visible_site_ids

    ids = visible_site_ids(user)
    rollouts = list(rollouts)
    if ids is None:
        return rollouts
    hosts = None
    out = []
    for rollout in rollouts:
        if rollout.site_id is not None:
            if rollout.site_id in ids:
                out.append(rollout)
            continue
        if rollout.host_ids is None:
            # Every host at the time it ran: the caller's are among them.
            out.append(rollout)
            continue
        if hosts is None:
            hosts = _visible_host_ids(user)
        if hosts & {str(h) for h in rollout.host_ids}:
            out.append(rollout)
    return out


def rollout_visible(user, rollout) -> bool:
    return bool(visible_rollouts([rollout], user))


def rollout_write_denied(user, rollout) -> Response | None:
    """None when *user* may halt, resume or skip *rollout*, else the response.

    Out of view answers 404 like an unknown id. A site's rollout needs
    ``tasks:run`` in that site. One without a site needs a fleet-wide runner,
    unless it names its hosts and the caller may run tasks on every one of
    them (a global policy a site admin ran on their own hosts, decisions-06
    item 4)."""
    from apps.accounts.permissions import can

    if not rollout_visible(user, rollout):
        return Response({"detail": "Not found"}, status=404)
    if rollout.site_id is None:
        if rollout.host_ids:
            from apps.hosts.models import Host
            targets = list(Host.objects.unscoped(
                reason="rollout writes: every host a rollout names must be runnable by the caller"
            ).filter(pk__in=rollout.host_ids))
            if targets and all(_can_run(user, h) for h in targets):
                return None
        return fleet_runner_denied(user)
    if not can(user, rollout.site_id, "tasks", "run"):
        return Response({"detail": "You may not act on this site's rollouts"}, status=403)
    return None
