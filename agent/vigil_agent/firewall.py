"""Firewall backends — ufw, firewall-cmd, Windows.

One interface over three tools, following the shape pkg_manager.py uses:
detect the available tool once, then dispatch per tool. Every backend returns
the same snapshot contract so the server has a single format to parse:

    {"tool": "ufw", "enabled": True,
     "defaults": {"incoming": "deny", "outgoing": "allow"},
     "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                "source": "any", "interface": ""}],
     "unparsed": []}

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

    def remove_rule(self, port, protocol, action="allow", source="any") -> str:
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
        if source and source != "any":
            cmd += ["from", source, "to", "any", "port", str(port),
                    "proto", protocol]
        else:
            cmd += [f"{port}/{protocol}"]
        return _run(cmd)

    def remove_rule(self, port, protocol, action="allow", source="any"):
        # `action` is threaded through because `ufw delete` matches the whole
        # rule: deleting a deny rule with "allow" in the pattern matches
        # nothing and reports success.
        cmd = ["ufw", "delete", action]
        if source and source != "any":
            cmd += ["from", source, "to", "any", "port", str(port),
                    "proto", protocol]
        else:
            cmd += [f"{port}/{protocol}"]
        return _run(cmd)


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

    def add_rule(self, port, protocol, action, source="any", interface=""):
        if action == "deny":
            # firewalld expresses "not allowed" by absence -- there is no
            # deny list to add to. Removing an allow is not the same thing
            # as adding a deny (and is a silent no-op if the port was never
            # open), so an explicit deny always adds a reject rich rule,
            # scoped to `source` when one is given.
            if source != "any":
                rule = (f'rule family="ipv4" source address="{source}" '
                        f'port port="{port}" protocol="{protocol}" reject')
            else:
                rule = (f'rule family="ipv4" '
                        f'port port="{port}" protocol="{protocol}" reject')
            out = _run(["firewall-cmd", "--permanent",
                       f"--add-rich-rule={rule}"])
        else:
            out = _run(["firewall-cmd", "--permanent",
                        f"--add-port={port}/{protocol}"])
        return f"{out}\n{self._reload()}".strip()

    def remove_rule(self, port, protocol, action="allow", source="any"):
        out = _run(["firewall-cmd", "--permanent",
                    f"--remove-port={port}/{protocol}"])
        return f"{out}\n{self._reload()}".strip()


_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

# Joins each enabled inbound rule to its port filter. Rules with no specific
# local port (LocalPort = "Any") are skipped: they are service-wide rules that
# do not map onto Vigil's port/protocol model, and showing them as port 0 would
# be a lie.
_PS_RULES = (
    "$o = Get-NetFirewallRule -Enabled True -Direction Inbound "
    "-ErrorAction SilentlyContinue | ForEach-Object { "
    "$f = $_ | Get-NetFirewallPortFilter; "
    "if ($f.LocalPort -and $f.LocalPort -ne 'Any') { "
    "[PSCustomObject]@{ port = $f.LocalPort; protocol = $f.Protocol; "
    "action = $_.Action.ToString(); name = $_.DisplayName } } }; "
    "$o | ConvertTo-Json -Compress"
)
_PS_PROFILES = ("Get-NetFirewallProfile | "
                "Select-Object Name,Enabled | ConvertTo-Json -Compress")


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

    def snapshot(self) -> dict:
        profiles: list[dict] = []
        unparsed: list[str] = []
        profiles_ok = False
        try:
            items, discarded = _as_list(json.loads(_run(_PS + [_PS_PROFILES])))
            for p in items:
                profiles.append({"name": str(p.get("Name", "")),
                                 "enabled": bool(p.get("Enabled"))})
            for d in discarded:
                unparsed.append(f"<unrecognised profile entry: {d!r}>")
            profiles_ok = True
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
                    name = str(r.get("name", "")).strip() or "<unnamed rule>"
                    unparsed.append(f"{name}: port={port}")
                    continue
                raw_action = str(r.get("action", "")).strip()
                action = raw_action.lower()
                name = str(r.get("name", "")).strip() or "<unnamed rule>"
                # Map explicitly rather than defaulting unknown actions to
                # "allow": rounding a rule we don't understand toward
                # "permitted" would understate the host's exposure, which is
                # the one direction a security view must never guess in.
                if action.startswith("block"):
                    mapped_action = "deny"
                elif action.startswith("allow"):
                    mapped_action = "allow"
                else:
                    unparsed.append(f"{name}: action={raw_action or '<empty>'}")
                    continue
                rules.append({
                    "port": int(port),
                    "protocol": str(r.get("protocol", "tcp")).lower(),
                    "action": mapped_action,
                    "source": "any", "interface": "",
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
            "defaults": {"incoming": "deny", "outgoing": "allow"},
            "profiles": profiles,
            "rules": rules,
            "unparsed": unparsed,
        }

    def add_rule(self, port, protocol, action, source="any", interface=""):
        name = f"Vigil {protocol}/{port}"
        cmd = ["netsh", "advfirewall", "firewall", "add", "rule",
               f"name={name}", "dir=in",
               f"action={'block' if action == 'deny' else 'allow'}",
               f"protocol={protocol.upper()}", f"localport={port}"]
        if source and source != "any":
            cmd.append(f"remoteip={source}")
        return _run(cmd)

    def remove_rule(self, port, protocol, action="allow", source="any"):
        # Matched on protocol and port rather than name, so a rule created
        # outside Vigil can still be removed.
        return _run(["netsh", "advfirewall", "firewall", "delete", "rule",
                     "name=all", f"protocol={protocol.upper()}",
                     f"localport={port}"])


def detect() -> FirewallBackend | None:
    """Return the backend for this host, or None if no supported tool exists."""
    for backend in (WindowsBackend(), UfwBackend(), FirewallCmdBackend()):
        if backend.available():
            return backend
    return None
