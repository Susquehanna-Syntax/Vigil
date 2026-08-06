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
    """ConvertTo-Json emits a bare object when there is exactly one result."""
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


class WindowsBackend(FirewallBackend):
    name = "windows"

    def available(self) -> bool:
        return sys.platform == "win32"

    def snapshot(self) -> dict:
        profiles: list[dict] = []
        unparsed: list[str] = []
        try:
            for p in _as_list(json.loads(_run(_PS + [_PS_PROFILES]))):
                profiles.append({"name": str(p.get("Name", "")),
                                 "enabled": bool(p.get("Enabled"))})
        except (ValueError, TypeError):
            logger.warning("Could not read Windows firewall profiles")
            # A snapshot that failed to read anything has to look different
            # from a host that legitimately has no profiles.
            unparsed.append("<failed to read Windows firewall profiles>")

        rules: list[dict] = []
        try:
            for r in _as_list(json.loads(_run(_PS + [_PS_RULES]))):
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
                action = str(r.get("action", "")).lower()
                rules.append({
                    "port": int(port),
                    "protocol": str(r.get("protocol", "tcp")).lower(),
                    "action": "deny" if action.startswith("block") else "allow",
                    "source": "any", "interface": "",
                })
        except (ValueError, TypeError):
            logger.warning("Could not read Windows firewall rules")
            unparsed.append("<failed to read Windows firewall rules>")

        return {
            "tool": self.name,
            "enabled": any(p["enabled"] for p in profiles),
            "defaults": {"incoming": "deny", "outgoing": "allow"},
            "profiles": profiles,
            "rules": rules,
            "unparsed": unparsed,
        }


def detect() -> FirewallBackend | None:
    """Return the backend for this host, or None if no supported tool exists."""
    for backend in (WindowsBackend(), UfwBackend(), FirewallCmdBackend()):
        if backend.available():
            return backend
    return None
