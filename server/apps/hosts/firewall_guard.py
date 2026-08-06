"""Refuse firewall changes that would cut off access to the host.

Applied to the Firewall tab's write endpoint only (Task 9). The task editor
path is deliberately unguarded: a rule Vigil will not write from a form is
still one you can write on purpose, and that escape hatch is what makes
refusing here reasonable rather than paternalistic.

The protected ports are the well-known ones (22, plus 3389 on Windows), not
values read from the host. Detecting a real sshd port costs another round
trip, and being wrong in the permissive direction is the failure that
matters -- so a host on a custom SSH port is simply not protected here, and
this is a deliberate, documented limitation.

``check_change`` must never raise. Task 9's endpoint calls it before doing
anything else, so a malformed or partial snapshot -- missing keys, an
``unparsed`` bucket instead of a parsed rule, ``"unknown"`` defaults, a
tri-state ``enabled`` of ``None`` on Windows -- must fall out as a refusal or
an allow, never a traceback. Where the snapshot does not give enough
information to be sure a change is safe, the guard refuses: it refuses more
when it knows less, never the other way around. In particular, a host whose
SSH access comes from an app-profile rule (`ufw allow OpenSSH`) has no
port-22 entry in `rules` -- that rule is only visible in `unparsed`, which
this guard cannot interpret -- so `default deny incoming` against such a
snapshot is refused. That is the correct, safe direction, even though it
produces a false refusal for a host that is really fine; do not "fix" it
into permissiveness.
"""
from __future__ import annotations

SSH_PORT = 22
RDP_PORT = 3389

_LOCKOUT_POLICIES = ("deny", "reject")


def _protected_ports(host, snapshot: dict) -> set[int]:
    ports = {SSH_PORT}
    os_name = (getattr(host, "os", "") or "").lower()
    tool = snapshot.get("tool", "")
    if "windows" in os_name or tool == "windows":
        ports.add(RDP_PORT)
    return ports


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _params(change: dict) -> dict:
    params = change.get("params")
    return params if isinstance(params, dict) else {}


def check_change(host, snapshot, change) -> str:
    """Return "" if the change is allowed, else a sentence saying why not."""
    change = change if isinstance(change, dict) else {}
    action = change.get("action", "")
    params = _params(change)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    protected = _protected_ports(host, snapshot)

    if action == "set_firewall_policy":
        direction = str(params.get("direction", "")).lower()
        pol = str(params.get("policy", "")).lower()

        if direction == "outgoing" and pol in _LOCKOUT_POLICIES:
            return (
                "Refusing to set the default outgoing policy to "
                f"{pol}. Vigil agents are outbound-only, so this host "
                "would stop being able to check in, and no task could be "
                "dispatched to undo it -- recovering would need console "
                "access. Write it as a task by hand if you really mean it.")

        if direction == "incoming" and pol in _LOCKOUT_POLICIES:
            raw_rules = snapshot.get("rules")
            rules = raw_rules if isinstance(raw_rules, list) else []
            allowed = {
                r.get("port") for r in rules
                if isinstance(r, dict) and r.get("action") == "allow"
            }
            missing = sorted(p for p in protected if p not in allowed)
            if missing:
                return (
                    f"Refusing to set the default incoming policy to "
                    f"{pol}: no allow rule covers port "
                    f"{', '.join(map(str, missing))}, so remote access to "
                    f"this host would be cut off. Add the allow rule first.")
        return ""

    if action == "add_firewall_rule":
        port = _as_int(params.get("port"))
        if str(params.get("action", "allow")).lower() == "deny" \
                and port in protected:
            return (
                f"Refusing to deny port {port} -- that is how this host is "
                f"reached, and Vigil could not undo it remotely. Write it "
                f"as a task by hand if you really mean it.")
        return ""

    if action == "remove_firewall_rule":
        port = _as_int(params.get("port"))
        if port in protected:
            return (
                f"Refusing to remove the rule permitting port {port} -- "
                f"that is how this host is reached. Write it as a task by "
                f"hand if you really mean it.")
        return ""

    # enable_firewall / disable_firewall are not lockouts. Disabling opens
    # the host rather than closing it: high risk, and audited, but not
    # refused here.
    return ""
