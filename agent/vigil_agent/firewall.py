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


def detect() -> FirewallBackend | None:
    """Return the backend for this host, or None if no supported tool exists."""
    for backend in (UfwBackend(),):
        if backend.available():
            return backend
    return None
