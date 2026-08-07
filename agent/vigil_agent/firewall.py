"""Firewall backends — ufw, firewall-cmd, Windows.

One interface over three tools, following the shape pkg_manager.py uses:
detect the available tool once, then dispatch per tool. Every backend returns
the same snapshot contract so the server has a single format to parse:

    {"tool": "ufw", "enabled": True,
     "defaults": {"incoming": "deny", "outgoing": "allow"},
     "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                "source": "any", "interface": "", "name": "", "rule_id": ""}],
     "unparsed": []}

``name`` is a rule's human-readable label (Windows DisplayName; empty on
ufw/firewall-cmd, which have none). ``rule_id`` is a rule's *unique*
identifier (Windows' Name/InstanceID property; likewise empty on
ufw/firewall-cmd). The two are never interchangeable: DisplayName is NOT
guaranteed unique on Windows -- WindowsBackend.add_rule's own naming scheme
can produce two rules sharing a DisplayName (e.g. an allow and a scoped deny
on the same port) -- so removal must key on ``rule_id``, never on ``name``.

``profiles`` is added by the Windows backend only, where firewall state is
per-profile and the profiles can disagree with each other.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys

logger = logging.getLogger("vigil.firewall")

_TIMEOUT = 30

#: An IPv4/IPv6 address or CIDR, or the literal "any". Deliberately strict:
#: this value reaches a command line.
_SAFE_SOURCE = re.compile(r"^(any|[0-9a-fA-F:.]{2,45}(/\d{1,3})?)$")
_SAFE_IFACE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,15}$")

#: Windows Firewall DisplayNames are free text (they can legitimately contain
#: spaces, parentheses, slashes -- e.g. "Core Networking - Multicast Listener
#: Query (ICMPv6-In)" or Vigil's own "Vigil tcp/445"), so this is a denylist
#: of characters that matter to PowerShell rather than an allowlist: quotes
#: (breaking out of the single-quoted literal the name is embedded in),
#: backticks (PowerShell's escape character), `$` (variable/subexpression
#: expansion), `;`/`|`/`&` (statement/pipeline/background separators), and
#: newlines. This value reaches a PowerShell command line.
_UNSAFE_RULE_NAME_CHARS = re.compile(r"[\"'`$;|&\r\n]")
_MAX_RULE_NAME_LEN = 255


def validate_source(source: str) -> str:
    source = (source or "any").strip()
    if not _SAFE_SOURCE.match(source):
        raise ValueError(f"Invalid source address: {source!r}")
    return source


def validate_interface(iface: str) -> str:
    iface = (iface or "").strip()
    if iface and not _SAFE_IFACE.match(iface):
        raise ValueError(f"Invalid interface: {iface!r}")
    return iface


def validate_rule_name(name: str) -> str:
    """Validate a firewall rule identifier string before it reaches PowerShell.

    Used for both a Windows rule's DisplayName and its unique Name
    (InstanceID) -- whichever string is about to be interpolated into a
    PowerShell command gets the same treatment, since both come from the
    host (or from a caller echoing back what a snapshot reported) and are
    therefore untrusted. A string that fails validation is refused outright
    -- never sanitised and passed through -- matching the discipline the
    rest of this module already applies to source/interface.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Rule identifier is required")
    if len(name) > _MAX_RULE_NAME_LEN:
        raise ValueError(f"Rule identifier too long ({len(name)} characters)")
    if _UNSAFE_RULE_NAME_CHARS.search(name):
        raise ValueError(f"Invalid rule identifier: {name!r}")
    return name


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> str:
    """Run a command and return stdout+stderr. Never uses a shell.

    Separate from executor._run because a read must not raise on a non-zero
    exit: `ufw status` on a host where ufw is installed but inactive is a
    perfectly good answer, not a failure.
    """
    result = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout, shell=False)
    return (result.stdout + result.stderr).strip()


class FirewallBackend:
    name = ""

    def available(self) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def add_rule(self, port, protocol, action, source="any", interface="") -> str:
        raise NotImplementedError

    def remove_rule(self, port, protocol, action="allow", source="any",
                    name="", rule_id="") -> str:
        raise NotImplementedError

    def set_policy(self, direction, policy) -> str:
        raise NotImplementedError

    def set_enabled(self, enabled: bool) -> str:
        raise NotImplementedError


class UfwBackend(FirewallBackend):
    name = "ufw"

    # "22/tcp   ALLOW IN   Anywhere" / "8080/tcp  DENY IN  10.0.0.0/8"
    # "22/tcp on eth0   ALLOW IN   Anywhere" / "25   ALLOW IN   Anywhere"
    _RULE = re.compile(
        r"^(?P<port>\d+)(?:/(?P<proto>tcp|udp))?"
        r"(?:\s+on\s+(?P<iface>\S+))?"
        r"\s+(?:\(v6\)\s+)?"
        r"(?P<action>ALLOW|DENY|REJECT)\s+(?:IN|OUT)\s+(?P<source>.+?)\s*$",
        re.I)
    _DEFAULTS = re.compile(
        r"^Default:\s*(?P<in>\w+)\s*\(incoming\),\s*(?P<out>\w+)\s*\(outgoing\)",
        re.I | re.M)
    # Status/table-header lines that can precede the rule rows -- never
    # treated as unparsed rules even though they aren't matched by _RULE.
    _HEADER = re.compile(
        r"^(Status:|Logging:|Default:|New profiles:|To\s+Action\s+From|--\s+------\s+----)",
        re.I)

    def available(self) -> bool:
        return shutil.which("ufw") is not None

    def snapshot(self) -> dict:
        out = _run(["ufw", "status", "verbose"])
        enabled = "status: active" in out.lower()
        defaults = {"incoming": "unknown", "outgoing": "unknown"}
        m = self._DEFAULTS.search(out)
        if m:
            defaults = {"incoming": m.group("in").lower(),
                        "outgoing": m.group("out").lower()}

        rules: list[dict] = []
        unparsed: list[str] = []
        seen: set[tuple] = set()
        for line in out.splitlines():
            if "(v6)" in line:
                continue      # dual-stack duplicate of a rule already listed
            stripped = line.strip()
            if not stripped or self._HEADER.match(stripped):
                continue      # blank lines and Status/Logging/Default/table headers
            m = self._RULE.match(stripped)
            if not m:
                # A row in the rules table we can't parse -- app profiles
                # ("OpenSSH  ALLOW IN  Anywhere") are the main case. Surface
                # it instead of dropping it: a firewall view that omits
                # rules is worse than one that admits it does not
                # understand them.
                unparsed.append(stripped)
                continue
            source = m.group("source").strip()
            proto = m.group("proto")
            rule = {
                "port": int(m.group("port")),
                "protocol": proto.lower() if proto else "any",
                "action": m.group("action").lower(),
                "source": "any" if source.lower().startswith("anywhere") else source,
                "interface": m.group("iface") or "",
                # ufw has no per-rule identity to read back -- ufw delete
                # matches on the rule's fields, not a name or an id. Both
                # carried as "" so every backend's rule dicts share the
                # same keys.
                "name": "",
                "rule_id": "",
            }
            key = (rule["port"], rule["protocol"], rule["action"],
                   rule["source"], rule["interface"])
            if key in seen:
                continue
            seen.add(key)
            rules.append(rule)

        return {"tool": self.name, "enabled": enabled,
                "defaults": defaults, "rules": rules, "unparsed": unparsed}

    def add_rule(self, port, protocol, action, source="any", interface=""):
        cmd = ["ufw", action]
        if interface:
            cmd += ["in", "on", interface]
        if (source and source != "any") or interface:
            # ufw's documented syntax pairs `in on IFACE` with the full
            # `from ... to any port PORT proto PROTO` form -- mixing `in on
            # IFACE` with the short PORT/PROTO form does not reliably parse.
            # So the full form is used whenever an interface is given, even
            # with no source, not just when a source is given.
            cmd += ["from", source or "any", "to", "any", "port", str(port),
                    "proto", protocol]
        else:
            cmd += [f"{port}/{protocol}"]
        return _run(cmd)

    def remove_rule(self, port, protocol, action="allow", source="any",
                    name="", rule_id=""):
        # `action` is threaded through because `ufw delete` matches the whole
        # rule: deleting a deny rule with "allow" in the pattern matches
        # nothing and reports success. `name`/`rule_id` are accepted for
        # contract uniformity with the other backends but unused -- ufw has
        # no rule identity to remove by, only the field-based match below.
        cmd = ["ufw", "delete", action]
        if source and source != "any":
            cmd += ["from", source, "to", "any", "port", str(port),
                    "proto", protocol]
        else:
            cmd += [f"{port}/{protocol}"]
        return _run(cmd)

    def set_policy(self, direction, policy):
        return _run(["ufw", "default", policy, direction])

    def set_enabled(self, enabled):
        # --force skips ufw's interactive "this may disrupt SSH" prompt; the
        # agent has no tty to answer it on.
        return _run(["ufw", "--force", "enable"] if enabled
                    else ["ufw", "disable"])


class FirewallCmdBackend(FirewallBackend):
    name = "firewall-cmd"

    _PORTS = re.compile(r"^\s*ports:\s*(?P<ports>.*)$", re.M)
    _PORT = re.compile(r"^(?P<port>\d+)/(?P<proto>tcp|udp)$", re.I)

    def available(self) -> bool:
        return shutil.which("firewall-cmd") is not None

    def snapshot(self) -> dict:
        enabled = _run(["firewall-cmd", "--state"]).strip().lower() == "running"
        listing = _run(["firewall-cmd", "--list-all"])

        rules: list[dict] = []
        unparsed: list[str] = []
        m = self._PORTS.search(listing)
        if m:
            for token in m.group("ports").split():
                pm = self._PORT.match(token.strip())
                if not pm:
                    # Not a <port>/<proto> pair -- most commonly a port
                    # *range* like "8000-8100/tcp", which firewalld allows
                    # but this feature can't express as a single rule.
                    # Surface it instead of dropping it: a firewall view
                    # that omits rules is worse than one that admits it
                    # does not understand them.
                    unparsed.append(token.strip())
                    continue
                # firewalld lists what is permitted. There is no deny list to
                # read back, so every parsed port is an allow.
                rules.append({
                    "port": int(pm.group("port")),
                    "protocol": pm.group("proto").lower(),
                    "action": "allow", "source": "any", "interface": "",
                    # firewalld has no per-rule identity to read back either
                    # -- both carried as "" for contract uniformity across
                    # backends.
                    "name": "", "rule_id": "",
                })

        # firewalld's default is to drop what is not listed; it has no
        # per-direction default policy in the ufw sense.
        return {"tool": self.name, "enabled": enabled,
                "defaults": {"incoming": "deny", "outgoing": "allow"},
                "rules": rules, "unparsed": unparsed}

    def _reload(self) -> str:
        # --permanent writes configuration; without a reload the running
        # firewall is untouched and the caller is told it succeeded.
        return _run(["firewall-cmd", "--reload"])

    @staticmethod
    def _reject_rich_rule(port, protocol, source="any") -> str:
        # firewalld expresses "not allowed" by absence -- there is no deny
        # list to add to. An explicit deny is instead a reject rich rule,
        # scoped to `source` when one is given. Shared by add_rule and
        # remove_rule so the rule string used to add a deny and the one used
        # to remove it can never drift apart -- a remove that doesn't match
        # what add actually wrote is a silent no-op reported as success.
        if source != "any":
            return (f'rule family="ipv4" source address="{source}" '
                    f'port port="{port}" protocol="{protocol}" reject')
        return (f'rule family="ipv4" '
                f'port port="{port}" protocol="{protocol}" reject')

    def add_rule(self, port, protocol, action, source="any", interface=""):
        if action == "deny":
            rule = self._reject_rich_rule(port, protocol, source)
            out = _run(["firewall-cmd", "--permanent",
                       f"--add-rich-rule={rule}"])
        else:
            out = _run(["firewall-cmd", "--permanent",
                        f"--add-port={port}/{protocol}"])
        return f"{out}\n{self._reload()}".strip()

    def remove_rule(self, port, protocol, action="allow", source="any",
                    name="", rule_id=""):
        # `action` is threaded through because a deny was added as a rich
        # rule, not a port grant: removing it with --remove-port never
        # touches the rich rule and is a silent no-op reported as success --
        # the same bug class this task exists to fix, in the sibling
        # backend. `name`/`rule_id` are accepted for contract uniformity but
        # unused -- firewalld rules are removed by their field-based match,
        # same as ufw.
        if action == "deny":
            rule = self._reject_rich_rule(port, protocol, source)
            out = _run(["firewall-cmd", "--permanent",
                       f"--remove-rich-rule={rule}"])
        else:
            out = _run(["firewall-cmd", "--permanent",
                        f"--remove-port={port}/{protocol}"])
        return f"{out}\n{self._reload()}".strip()

    def set_policy(self, direction, policy):
        if direction != "incoming":
            raise ValueError(
                "firewalld has no separate outgoing default policy")
        target = {"allow": "ACCEPT", "deny": "DROP", "reject": "REJECT"}[policy]
        out = _run(["firewall-cmd", "--permanent", f"--set-target={target}"])
        return f"{out}\n{self._reload()}".strip()

    def set_enabled(self, enabled):
        return _run(["systemctl", "start" if enabled else "stop", "firewalld"])


_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

# Joins each enabled inbound rule to its port filter. Rules with no specific
# local port (LocalPort = "Any") are skipped: they are service-wide rules that
# do not map onto Vigil's port/protocol model, and showing them as port 0 would
# be a lie.
#
# `name` is DisplayName -- human-readable, NOT guaranteed unique (two rules
# can legitimately share one; add_rule's own naming scheme used to make that
# the common case for an allow + a scoped deny on the same port).
# `rule_id` is Name -- the InstanceID, which IS unique. remove_rule must key
# on `rule_id`, never on `name`.
_PS_RULES = (
    "$o = Get-NetFirewallRule -Enabled True -Direction Inbound "
    "-ErrorAction SilentlyContinue | ForEach-Object { "
    "$f = $_ | Get-NetFirewallPortFilter; "
    "if ($f.LocalPort -and $f.LocalPort -ne 'Any') { "
    "[PSCustomObject]@{ port = $f.LocalPort; protocol = $f.Protocol; "
    "action = $_.Action.ToString(); name = $_.DisplayName; "
    "rule_id = $_.Name } } }; "
    "$o | ConvertTo-Json -Compress"
)
_PS_PROFILES = (
    "Get-NetFirewallProfile | Select-Object Name,Enabled,"
    "DefaultInboundAction,DefaultOutboundAction | ConvertTo-Json -Compress"
)


def _as_list(parsed):
    """ConvertTo-Json emits a bare object when there is exactly one result.

    Returns (dicts, discarded): `dicts` is every list/object entry that is
    actually a dict (the only shape callers can `.get()` off of); `discarded`
    holds anything else -- a bare scalar (PowerShell emits e.g. a lone `5`
    for some single-property results), so callers can record it in
    `unparsed` rather than crashing on `.get()` or dropping it unseen.
    """
    if parsed is None:
        return [], []
    items = parsed if isinstance(parsed, list) else [parsed]
    dicts = [i for i in items if isinstance(i, dict)]
    discarded = [i for i in items if not isinstance(i, dict)]
    return dicts, discarded


class WindowsBackend(FirewallBackend):
    name = "windows"

    def available(self) -> bool:
        return sys.platform == "win32"

    @staticmethod
    def _map_defaults(items: list[dict]) -> dict:
        """Map parsed Get-NetFirewallProfile entries onto Vigil's
        "allow"/"deny" default-policy vocabulary.

        Profiles can disagree with each other (Domain vs Private vs Public),
        and a value can be "NotConfigured" or something this backend does not
        recognise. Either case collapses to "unknown" for that direction --
        picking one profile's value arbitrarily would silently misreport the
        host's real exposure, which is the one direction a security view must
        never guess in.
        """
        def _one(key: str) -> str:
            values = {str(p.get(key, "")).strip().lower() for p in items}
            if len(values) != 1:
                return "unknown"
            return {"block": "deny", "allow": "allow"}.get(next(iter(values)), "unknown")

        return {"incoming": _one("DefaultInboundAction"),
                "outgoing": _one("DefaultOutboundAction")}

    def _read_defaults(self) -> dict:
        """Read the host's actual per-direction default policy.

        Used by set_policy so that changing one direction never has to guess
        at (and risk clobbering) the other -- see set_policy's docstring.
        Failure modes mirror snapshot(): a read that fails outright, or a
        host with no profiles at all, reports "unknown" for both directions
        rather than a guessed value.
        """
        try:
            items, _discarded = _as_list(json.loads(_run(_PS + [_PS_PROFILES])))
        except (ValueError, TypeError):
            items = []
        if not items:
            return {"incoming": "unknown", "outgoing": "unknown"}
        return self._map_defaults(items)

    def snapshot(self) -> dict:
        profiles: list[dict] = []
        unparsed: list[str] = []
        profiles_ok = False
        defaults = {"incoming": "unknown", "outgoing": "unknown"}
        try:
            items, discarded = _as_list(json.loads(_run(_PS + [_PS_PROFILES])))
            for p in items:
                profiles.append({"name": str(p.get("Name", "")),
                                 "enabled": bool(p.get("Enabled"))})
            for d in discarded:
                unparsed.append(f"<unrecognised profile entry: {d!r}>")
            profiles_ok = True
            defaults = self._map_defaults(items)
        except (ValueError, TypeError):
            logger.warning("Could not read Windows firewall profiles")
            # A snapshot that failed to read anything has to look different
            # from a host that legitimately has no profiles.
            unparsed.append("<failed to read Windows firewall profiles>")

        rules: list[dict] = []
        try:
            items, discarded = _as_list(json.loads(_run(_PS + [_PS_RULES])))
            for d in discarded:
                unparsed.append(f"<unrecognised rule entry: {d!r}>")
            for r in items:
                port = str(r.get("port", "")).strip()
                if not port.isdigit():
                    # Port ranges ("8000-8100") and comma-separated lists
                    # ("80,443") are valid Windows Firewall rules but this
                    # feature can only express a single port per rule.
                    # Surface it instead of dropping it: a firewall view
                    # that omits rules is worse than one that admits it
                    # does not understand them.
                    display_name = str(r.get("name", "")).strip() or "<unnamed rule>"
                    unparsed.append(f"{display_name}: port={port}")
                    continue
                raw_action = str(r.get("action", "")).strip()
                action = raw_action.lower()
                # Kept distinct from the "<unnamed rule>" placeholder used
                # for display below: an empty `name`/`rule_id` here means
                # remove_rule correctly has nothing to identify the rule by
                # and refuses, rather than trying to remove a rule literally
                # named "<unnamed rule>".
                name = str(r.get("name", "")).strip()
                rule_id = str(r.get("rule_id", "")).strip()
                display_name = name or "<unnamed rule>"
                # Map explicitly rather than defaulting unknown actions to
                # "allow": rounding a rule we don't understand toward
                # "permitted" would understate the host's exposure, which is
                # the one direction a security view must never guess in.
                if action.startswith("block"):
                    mapped_action = "deny"
                elif action.startswith("allow"):
                    mapped_action = "allow"
                else:
                    unparsed.append(f"{display_name}: action={raw_action or '<empty>'}")
                    continue
                rules.append({
                    "port": int(port),
                    "protocol": str(r.get("protocol", "tcp")).lower(),
                    "action": mapped_action,
                    "source": "any", "interface": "",
                    # `name` (DisplayName) is shown to the operator.
                    # `rule_id` (the unique Name/InstanceID) is what
                    # remove_rule actually targets -- netsh has no action
                    # filter, so removing by port/protocol alone would
                    # delete every inbound rule on that port, and removing
                    # by `name` alone is not safe either: DisplayName is not
                    # guaranteed unique (see remove_rule below).
                    "name": name,
                    "rule_id": rule_id,
                })
        except (ValueError, TypeError):
            logger.warning("Could not read Windows firewall rules")
            unparsed.append("<failed to read Windows firewall rules>")

        # An unknown firewall state must never render as a known one: if the
        # profile read failed, `enabled` is None (unknown), not False. A
        # status pill that says "disabled" for a host we simply couldn't
        # read from would be a confident wrong answer -- worse than "unknown".
        enabled = any(p["enabled"] for p in profiles) if profiles_ok else None

        return {
            "tool": self.name,
            "enabled": enabled,
            "defaults": defaults,
            "profiles": profiles,
            "rules": rules,
            "unparsed": unparsed,
        }

    def add_rule(self, port, protocol, action, source="any", interface=""):
        # The DisplayName includes the disposition (and the source, when
        # scoped) so two rules Vigil creates on the same port -- e.g. a
        # general allow and a later scoped deny -- do not collide on their
        # display name. This is defense in depth only, not the removal
        # safeguard itself: DisplayName is still not guaranteed unique in
        # general (nothing stops a rule created outside Vigil, or an older
        # Vigil rule from before this change, from sharing one), so
        # remove_rule below keys on the rule's unique Name/InstanceID
        # (rule_id), never on this DisplayName.
        disposition = "deny" if action == "deny" else "allow"
        name = f"Vigil {protocol}/{port} {disposition}"
        if source and source != "any":
            name += f" from {source}"
        cmd = ["netsh", "advfirewall", "firewall", "add", "rule",
               f"name={name}", "dir=in",
               f"action={'block' if action == 'deny' else 'allow'}",
               f"protocol={protocol.upper()}", f"localport={port}"]
        if source and source != "any":
            cmd.append(f"remoteip={source}")
        return _run(cmd)

    def remove_rule(self, port, protocol, action="allow", source="any",
                    name="", rule_id=""):
        # netsh has NO action filter: "delete rule name=all dir=in
        # protocol=X localport=Y" deletes every inbound rule on that port,
        # allows and denies alike. A port carrying a general allow plus a
        # scoped deny (blocking one bad source -- a normal pattern, since
        # block rules take precedence over allow) would lose BOTH on one
        # remove. Under a default-allow inbound policy -- which this same
        # backend lets an operator set via set_policy -- losing that deny
        # re-permits the previously-blocked source. That is
        # security-loosening, so removal by port/protocol match is no
        # longer offered here at all.
        #
        # Removing by DisplayName (`name`) is NOT safe either, and is
        # deliberately not accepted here even though it is threaded through
        # this method's signature for contract uniformity with the other
        # backends: DisplayName is not guaranteed unique on Windows, and
        # add_rule's OWN naming scheme (before this fix) produced exactly
        # the allow+scoped-deny collision described above under the
        # identical name "Vigil tcp/<port>" -- so a name-keyed removal could
        # delete the wrong one of two same-named rules. Only Name
        # (InstanceID, surfaced here as `rule_id`) is guaranteed unique, so
        # that is what this removes by, via PowerShell -- matching how the
        # read path (snapshot/_PS_RULES) already uses PowerShell for
        # structured work.
        #
        # Without a rule_id there is nothing safe to match on, and guessing
        # (falling back to the old netsh command, or to DisplayName) is
        # exactly the over-match this exists to stop -- so this refuses
        # rather than guessing, the same discipline set_policy already
        # applies when the untouched direction can't be read, and the
        # lockout guard applies to actions it doesn't recognise.
        rule_id = (rule_id or "").strip()
        if not rule_id:
            raise RuntimeError(
                "Cannot remove a Windows firewall rule without its unique "
                "identifier: netsh has no action filter, so removing by "
                "port and protocol alone would delete every inbound rule "
                "on that port. Its DisplayName is not a safe substitute "
                "either -- DisplayName is not guaranteed unique, and two "
                "rules (e.g. an allow and a scoped deny on the same port) "
                "can legitimately share one. Pass the rule's rule_id (its "
                "unique Name/InstanceID, as reported by snapshot()) to "
                "remove it precisely."
            )
        rule_id = validate_rule_name(rule_id)
        return _run(_PS + [f"Remove-NetFirewallRule -Name '{rule_id}'"])

    def set_policy(self, direction, policy):
        # netsh's `firewallpolicy` setter changes BOTH directions in one
        # call -- there is no per-direction setter. A call that changes only
        # `direction` must therefore read the *other* direction's current
        # value first and restate it unchanged; hardcoding the untouched
        # direction (e.g. always "allowoutbound" when setting inbound) would
        # silently reset it, quietly undoing a policy an operator
        # deliberately set on a host whose outbound was restrictive.
        verb = {"allow": "allow", "deny": "block", "reject": "block"}[policy]
        current = self._read_defaults()
        netsh_verb = {"allow": "allow", "deny": "block"}

        other = "outgoing" if direction == "incoming" else "incoming"
        other_value = current[other]
        if other_value not in netsh_verb:
            # The direction we were NOT asked to change reads "unknown"
            # (profiles disagree with each other, or the PowerShell read
            # failed outright). "unknown" is not a value netsh accepts, and
            # there is no safe value to guess: netsh writes both directions
            # in a single call, so any guess here is applied to the host as
            # if it were the operator's real, deliberate policy -- and a
            # wrong guess of "allow" would silently loosen a host whose real
            # policy is a deliberate `deny`. Refusing is the only option
            # that cannot make things worse. This is the same discipline
            # this backend already applies elsewhere: `enabled` is `None`
            # rather than `False` when unknown, and unrecognised rule
            # actions go to `unparsed` rather than defaulting to "allow".
            # An unknown state must never be rendered as, or acted on as, a
            # known one.
            raise RuntimeError(
                f"Cannot set the {direction} firewall policy: the current "
                f"{other} policy could not be read (profiles may disagree "
                f"with each other, or the read failed) and netsh sets both "
                f"directions in a single call, so proceeding would mean "
                f"guessing at a {other} policy that may be deliberately "
                f"restrictive. Retry, or investigate why "
                f"Get-NetFirewallProfile could not be read on this host."
            )

        if direction == "incoming":
            in_verb = verb
            out_verb = netsh_verb[other_value]
        else:
            in_verb = netsh_verb[other_value]
            out_verb = verb

        return _run(["netsh", "advfirewall", "set", "allprofiles",
                     "firewallpolicy", f"{in_verb}inbound,{out_verb}outbound"])

    def set_enabled(self, enabled):
        return _run(["netsh", "advfirewall", "set", "allprofiles", "state",
                     "on" if enabled else "off"])


def detect() -> FirewallBackend | None:
    """Return the backend for this host, or None if no supported tool exists."""
    for backend in (WindowsBackend(), UfwBackend(), FirewallCmdBackend()):
        if backend.available():
            return backend
    return None
