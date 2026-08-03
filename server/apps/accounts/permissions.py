"""Authorisation: who may do what, where.

One question — ``can(user, site, app, verb)`` — answered in two stages:
resolve the role for that site, then ask what the role permits. See
docs/rbac.md.

Core never imports apps_business directly; per-site rows are read through
vigil.scoping, so an AGPL-only build resolves exactly as Vigil did before
sites existed.
"""
from rest_framework.permissions import BasePermission

from .models import Role

#: Effective roles that are never stored on a UserProfile. Kept out of
#: ``Role`` so the profile dropdown (and its migration) stays at three.
OWNER = "owner"
NONE = ""

#: The permission vocabulary. Fixed on purpose: a typo must raise, not deny.
CAPABILITIES = {
    "hosts": frozenset({"view", "edit", "approve", "delete"}),
    "tasks": frozenset({"view", "run"}),
    "baselines": frozenset({"view", "run", "edit"}),
    "automations": frozenset({"view", "edit", "toggle"}),
    "alerts": frozenset({"view", "ack", "silence"}),
    "statuspages": frozenset({"view", "edit"}),
    # Rebuild destroys data by design. Image management is deliberately NOT a
    # verb here: uploading an ISO chooses what runs as root on every rebuilt
    # machine, so it stays admin-only, like agent_dist.upload_agent.
    "reprovision": frozenset({"view", "rebuild"}),
}

#: What an OPERATOR could do before per-site capabilities existed: deploy and
#: execute, acknowledge alerts, see everything — but no settings and no
#: approvals. Applied to operators who hold NO site rows, so that turning the
#: feature on does not silently strip every existing operator. An operator
#: with a row consults the matrix instead, where absence means denial.
LEGACY_OPERATOR = frozenset({
    ("hosts", "view"),
    ("tasks", "view"), ("tasks", "run"),
    ("baselines", "view"), ("baselines", "run"),
    ("automations", "view"), ("automations", "toggle"),
    ("alerts", "view"), ("alerts", "ack"), ("alerts", "silence"),
    ("statuspages", "view"),
    # Deliberately no ("reprovision", …): rebuild is destructive and must be
    # granted explicitly per site, never inherited by operators who predate
    # the feature.
})


class Capability:
    """A validated (app, verb) pair. Constructing an invalid one raises."""

    __slots__ = ("app", "verb")

    def __init__(self, app: str, verb: str):
        _validate(app, verb)
        self.app = app
        self.verb = verb

    def as_tuple(self):
        return (self.app, self.verb)

    def __repr__(self):
        return f"Capability({self.app!r}, {self.verb!r})"


def _validate(app: str, verb: str) -> None:
    verbs = CAPABILITIES.get(app)
    if verbs is None:
        raise ValueError(
            f"unknown app {app!r}; known: {', '.join(sorted(CAPABILITIES))}")
    if verb not in verbs:
        raise ValueError(
            f"unknown verb {verb!r} for {app!r}; known: {', '.join(sorted(verbs))}")


def _unscoped_role(user) -> str:
    """The role a user has when no per-site rows apply — identical to the
    pre-sites behaviour, which is what keeps existing installs working."""
    if user.is_staff or user.is_superuser:
        return Role.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else Role.VIEWER


def role_of(user, site=None) -> str:
    """The effective role for `user` in `site` (docs/rbac.md §1.1).

    `site=None` asks the unscoped question, for areas that have no site:
    accounts, settings, licensing, the task library.
    """
    if not (user and user.is_authenticated):
        return NONE
    if user.is_superuser:
        return OWNER

    from vigil import scoping
    rows = scoping.site_roles_for(user)
    if not rows:
        return _unscoped_role(user)

    if site is not None:
        by_site = rows.get(getattr(site, "id", site))
        if by_site is not None:
            return by_site
        glob = rows.get("__global__")
        if glob is not None:
            return glob
        return NONE

    # Unscoped question from a scoped user: settings and account pages must
    # not become unreachable just because someone was given site rows. Answer
    # with their strongest role anywhere.
    ranked = [Role.ADMIN, Role.OPERATOR, Role.VIEWER]
    held = {r for k, r in rows.items()}
    for role in ranked:
        if role in held:
            return role
    return NONE


def can(user, site, app: str, verb: str) -> bool:
    """May `user` perform `app`:`verb` in `site`? Raises on an unknown
    app or verb so mistakes surface in tests, not as silent denials."""
    _validate(app, verb)
    if not (user and user.is_authenticated):
        return False

    role = role_of(user, site)
    if role == OWNER or role == Role.ADMIN:
        return True
    if role == Role.VIEWER:
        return verb == "view"
    if role == Role.OPERATOR:
        return _operator_can(user, site, app, verb)
    return False


def _operator_can(user, site, app: str, verb: str) -> bool:
    from vigil import scoping
    caps = scoping.capabilities_for(user, site)
    if caps is None:
        # No row backing this operator — they came from a profile role, so
        # they keep the powers operators had before the matrix existed.
        return (app, verb) in LEGACY_OPERATOR
    return (app, verb) in caps


class IsAdmin(BasePermission):
    """Server-side admin gate for administrative endpoints.

    Any user who isn't an admin must not be able to upload agent binaries,
    change settings, or decide enrollments.
    """

    message = "Administrator access required."

    def has_permission(self, request, view):
        return role_of(request.user) in (Role.ADMIN, OWNER)


class IsOperator(BasePermission):
    """Operator-or-better: may deploy/execute tasks and acknowledge alerts,
    but not change settings or approve hosts (those stay IsAdmin). The
    OPERATOR role itself is only assignable under a Business license
    (``rbac_advanced``); this check stays free because a licensed install's
    operators must keep working after a lapse — §6: degrade features, never
    lock humans out mid-incident."""

    message = "Operator access required."

    def has_permission(self, request, view):
        return role_of(request.user) in (Role.ADMIN, Role.OPERATOR, OWNER)


def visible_site_ids(user):
    """The site ids `user` may act in, or ``None`` meaning "every site".

    None and the empty set are different answers: None is an unscoped user,
    who sees everything exactly as they did before per-site roles existed;
    the empty set is a user scoped to nothing.
    """
    if not (user and user.is_authenticated):
        return set()
    if user.is_superuser:
        return None

    from vigil import scoping
    rows = scoping.site_roles_for(user)
    if not rows:
        return None
    if "__global__" in rows:
        # A global row is the floor, so it reaches every site.
        return None
    return {k for k in rows if k != "__global__"}
