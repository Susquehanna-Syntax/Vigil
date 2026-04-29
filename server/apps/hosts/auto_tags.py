"""Auto-tagging rules — derive tags from intrinsic host properties.

Auto-tags are evaluated during checkin (in addition to the agent.yml tags
the agent advertises) and during AD import. They never *remove* operator
tags; they only ensure derived facts are reflected.

The current ruleset:
    - OS family       (windows / linux / macos)
    - Mode            (managed / monitor / full-control)
    - AD OU segments  (OU=Servers,OU=IT  →  servers, it)

Add new rules by appending to ``AUTO_TAG_RULES``. Each rule is a callable
``(host, **context) -> Iterable[str]`` returning tag strings to merge.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import Host

AutoTagRule = Callable[..., Iterable[str]]


def _tag_os(host: Host, **_) -> Iterable[str]:
    os_lower = (host.os or "").lower()
    if not os_lower:
        return []
    if "windows" in os_lower:
        return ["windows"]
    if "darwin" in os_lower or "mac" in os_lower:
        return ["macos"]
    return ["linux"]


def _tag_mode(host: Host, **_) -> Iterable[str]:
    if host.mode == Host.Mode.FULL_CONTROL:
        return ["full-control"]
    if host.mode == Host.Mode.MANAGED:
        return ["managed"]
    if host.mode == Host.Mode.MONITOR:
        return ["monitor"]
    return []


def _tag_ad_ou(_host: Host, *, ad_distinguished_name: str | None = None, **_kw) -> Iterable[str]:
    """Extract OU segments from an AD computer object's DN.

    Example: ``CN=PC1,OU=Workstations,OU=IT,DC=example,DC=com`` → ``workstations, it``
    """
    if not ad_distinguished_name:
        return []
    out: list[str] = []
    for component in ad_distinguished_name.split(","):
        component = component.strip()
        if component.upper().startswith("OU="):
            value = component[3:].strip().lower()
            if value:
                out.append(value)
    return out


AUTO_TAG_RULES: list[AutoTagRule] = [_tag_os, _tag_mode, _tag_ad_ou]


def derive_auto_tags(host: Host, **context) -> list[str]:
    """Run every auto-tag rule and return a sorted, deduped tag list."""
    seen: set[str] = set()
    out: list[str] = []
    for rule in AUTO_TAG_RULES:
        try:
            for tag in rule(host, **context) or []:
                t = (tag or "").strip().lower()
                if not t or t in seen:
                    continue
                seen.add(t)
                out.append(t)
        except Exception:  # never let a bad rule break checkin
            continue
    return sorted(out)


def merge_auto_tags(host: Host, **context) -> list[str]:
    """Merge auto-tags into the existing host.tags list (additive, no removals)."""
    existing = list(host.tags or [])
    seen = {t.lower() for t in existing if isinstance(t, str)}
    for t in derive_auto_tags(host, **context):
        if t not in seen:
            existing.append(t)
            seen.add(t)
    return sorted(existing)
