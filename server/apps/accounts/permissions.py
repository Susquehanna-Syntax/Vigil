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
    # update_agent and firewall are their own verbs rather than folding into
    # "edit". Replacing the agent binary swaps the executable that enforces the
    # agent's own allowlist, and firewall rules decide what can reach a machine
    # at all — both are worth delegating deliberately rather than as a side
    # effect of being allowed to rename a host.
    # shell is its own verb too: a root shell on the machine is delegated
    # deliberately, never as a side effect of being allowed to edit a host.
    "hosts": frozenset({"view", "edit", "approve", "delete",
                        "update_agent", "firewall", "shell"}),
    "tasks": frozenset({"view", "run"}),
    "playbooks": frozenset({"view", "run", "edit"}),
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
    ("hosts", "view"), ("hosts", "edit"),
    ("tasks", "view"), ("tasks", "run"),
    ("playbooks", "view"), ("playbooks", "run"),
    ("automations", "view"), ("automations", "toggle"),
    ("alerts", "view"), ("alerts", "ack"), ("alerts", "silence"),
    ("statuspages", "view"),
    # Deliberately no ("reprovision", …): rebuild is destructive and must be
    # granted explicitly per site, never inherited by operators who predate
    # the feature. Same reasoning for ("hosts", "update_agent") and
    # ("hosts", "firewall") — both were reachable by anyone with a TOTP code
    # before this, and the fix is not to hand them back automatically.
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
    if site is not None and has_grant(user, app, verb):
        # Company-wide (S1 04f) for this app and verb, on any site's objects but
        # never the Global site's: global items need the Global scope's authority.
        from vigil import scoping
        if getattr(site, "id", site) != scoping.global_site_id():
            return True

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


#: What an instance admin may grant company-wide (S1 04f, decisions-06 item 6):
#: module -> verbs. ``view`` widens reads in that module only; ``run`` / ``ack``
#: let the user act on other sites' objects in it. Never a settings verb, and
#: never anything that reaches global items' writes.
GRANTABLE = {
    "hosts": frozenset({"view"}),
    "inventory": frozenset({"view"}),
    "software": frozenset({"view"}),
    "vulns": frozenset({"view"}),
    "alerts": frozenset({"view", "ack"}),
    "tasks": frozenset({"view", "run"}),
    "playbooks": frozenset({"view", "run"}),
    "automations": frozenset({"view"}),
    "stacks": frozenset({"view"}),
    "devices": frozenset({"view"}),
    "reprovision": frozenset({"view"}),
}


def has_grant(user, app, verb="view") -> bool:
    """Does *user* hold a company-wide grant on *app*? For ``view``, any verb on
    the app counts: someone who may act on another site's objects must see them.
    A site-scoped API key never carries a grant."""
    if not app or getattr(user, "key_site_ids", None) is not None:
        return False
    from vigil import scoping
    grants = scoping.grants_for(user)
    if verb == "view":
        return any(a == app for a, _ in grants)
    return (app, verb) in grants


def visible_site_ids(user, app=None):
    """The site ids `user` may act in, or ``None`` meaning "every site".

    None and the empty set are different answers: None is an unscoped user,
    who sees everything exactly as they did before per-site roles existed;
    the empty set is a user scoped to nothing.

    A request made with a Business scoped API key carries that key's sites
    on the user as ``key_site_ids``; the answer is then whatever the creator
    may see *within* those sites, so a key can narrow its creator and never
    widen them. The attribute lives on the per-request user object the key
    authentication returns, never on a stored row.
    """
    ids = _role_site_ids(user)
    if app is not None and ids is not None and has_grant(user, app):
        # A company-wide grant opens this one module (S1 04f).
        ids = None
    key_ids = getattr(user, "key_site_ids", None)
    if key_ids is None:
        return ids
    key_ids = set(key_ids)
    return key_ids if ids is None else ids & key_ids


def _role_site_ids(user):
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
