"""Firewall actions: add / remove a port rule, default policies, enable, list.

Moved out of executor.py. Each handler validates its own params and then
delegates the platform difference to the detected backend (ufw, firewall-cmd,
or Windows Defender Firewall) from ``vigil_agent.firewall``; a host with no
supported tool is an error for a change and a normal answer for a read — a
listing reports ``supported: false`` rather than failing the task. executor
re-exports every name defined here.
"""

from __future__ import annotations

import json

from .. import firewall
from ..config import AgentConfig
from ..executor import ActionOutput


def _add_firewall_rule(params: dict, _config: AgentConfig) -> str:
    port = int(params.get("port", 0))
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    protocol = str(params.get("protocol", "tcp")).lower()
    if protocol not in ("tcp", "udp"):
        raise ValueError(f"Protocol must be tcp or udp, got {protocol!r}")
    action = str(params.get("action", "allow")).lower()
    if action not in ("allow", "deny"):
        raise ValueError(f"Action must be allow or deny, got {action!r}")
    source = firewall.validate_source(params.get("source", "any"))
    interface = firewall.validate_interface(params.get("interface", ""))

    backend = firewall.detect()
    if backend is None:
        raise RuntimeError(
            "No supported firewall tool found (ufw, firewall-cmd, or Windows)")
    return ActionOutput(
        backend.add_rule(port, protocol, action, source, interface),
        {"port": str(port), "protocol": str(protocol), "action": str(action)})


def _remove_firewall_rule(params: dict, _config: AgentConfig) -> str:
    port = int(params.get("port", 0))
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    protocol = str(params.get("protocol", "tcp")).lower()
    if protocol not in ("tcp", "udp"):
        raise ValueError(f"Protocol must be tcp or udp, got {protocol!r}")
    action = str(params.get("action", "allow")).lower()
    if action not in ("allow", "deny", "reject"):
        raise ValueError(f"Action must be allow, deny or reject, got {action!r}")
    source = firewall.validate_source(params.get("source", "any"))
    # Optional: only WindowsBackend uses these, and it validates rule_id
    # itself (see firewall.validate_rule_name) before it ever reaches
    # PowerShell -- ufw and firewall-cmd ignore both. `name` (DisplayName)
    # is carried for display/back-compat only; WindowsBackend removes by
    # `rule_id` (the unique Name/InstanceID), never by `name` -- DisplayName
    # is not guaranteed unique. See firewall.WindowsBackend.remove_rule.
    name = str(params.get("name", "") or "")
    rule_id = str(params.get("rule_id", "") or "")

    backend = firewall.detect()
    if backend is None:
        raise RuntimeError(
            "No supported firewall tool found (ufw, firewall-cmd, or Windows)")
    return ActionOutput(
        backend.remove_rule(port, protocol, action, source,
                            name=name, rule_id=rule_id),
        {"port": str(port), "protocol": str(protocol), "action": str(action)})


def _set_firewall_policy(params: dict, _config: AgentConfig) -> str:
    direction = str(params.get("direction", "")).lower()
    if direction not in ("incoming", "outgoing"):
        raise ValueError(
            f"Direction must be incoming or outgoing, got {direction!r}")
    policy = str(params.get("policy", "")).lower()
    if policy not in ("allow", "deny", "reject"):
        raise ValueError(
            f"Policy must be allow, deny or reject, got {policy!r}")
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(
        backend.set_policy(direction, policy),
        {"direction": direction, "policy": policy})


def _enable_firewall(_params: dict, _config: AgentConfig) -> str:
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(backend.set_enabled(True), {"enabled": True})


def _disable_firewall(_params: dict, _config: AgentConfig) -> str:
    backend = firewall.detect()
    if backend is None:
        raise RuntimeError("No supported firewall tool found")
    return ActionOutput(backend.set_enabled(False), {"enabled": False})


def _list_firewall_rules(_params: dict, _config: AgentConfig) -> str:
    """Return this host's firewall state as JSON.

    A host with no supported firewall tool is a normal answer, not an error:
    raising here would mark the task failed and bury the one fact the operator
    needs behind a stack trace.
    """
    backend = firewall.detect()
    if backend is None:
        snapshot = {
            "tool": None, "supported": False, "enabled": False,
            "defaults": {"incoming": "unknown", "outgoing": "unknown"},
            "rules": [], "unparsed": [],
        }
        return ActionOutput(
            json.dumps(snapshot),
            {"supported": False, "enabled": False, "rule_count": 0})
    snapshot = backend.snapshot()
    snapshot["supported"] = True
    return ActionOutput(
        json.dumps(snapshot),
        {"supported": bool(snapshot.get("supported", True)),
         "enabled": bool(snapshot.get("enabled", False)),
         "rule_count": len(snapshot.get("rules", []))})
