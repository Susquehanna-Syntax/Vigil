"""Edition and feature gating — the second extension seam.

Vigil core is the free **Community** edition. The **Pro** and **Enterprise**
editions ship as separate repos (``Vigil-Pro``, ``Vigil-Enterprise``) whose
apps register the optional features they provide by calling
:func:`register_feature` from their ``AppConfig.ready()``.

Core code lights up integration points with :func:`feature_enabled` — e.g. a
template that shows a "Suggested fix" button only when ``ai_suggestions`` is
registered. Core never imports edition code: an absent edition just means the
feature was never registered, so it reads as off.

This is a *capability switch for UX and wiring*, not a license enforcement
mechanism. Self-hosted editions are gated by which app is installed; the
future Enterprise SaaS layers real licensing on top, in its own repo.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("vigil.editions")

COMMUNITY = "community"
PRO = "pro"
ENTERPRISE = "enterprise"  # the "Business" tier; SaaS in the future

# Which edition introduces each optional feature. Drives docs and upgrade
# prompts ("available in Pro"). Keep in sync with docs/EDITIONS.md.
FEATURE_EDITIONS: dict[str, str] = {
    # Pro (Vigil-Pro)
    "rbac": PRO,
    "baselines": PRO,
    "ai_suggestions": PRO,
    "status_pages": PRO,
    # Enterprise / Business (Vigil-Enterprise)
    "audit_log": ENTERPRISE,
    "sites": ENTERPRISE,
    "branding": ENTERPRISE,
    "sso": ENTERPRISE,
    "federation": ENTERPRISE,
}

_enabled: set[str] = set()


def register_feature(name: str) -> None:
    """Mark *name* as active. Called by an edition app at startup."""
    if name not in FEATURE_EDITIONS:
        logger.warning("Registering unknown edition feature %r", name)
    _enabled.add(name)
    logger.info("Edition feature enabled: %s", name)


def feature_enabled(name: str) -> bool:
    """True if an installed edition has registered *name*."""
    return name in _enabled


def enabled_features() -> set[str]:
    """The set of currently active optional features (copy)."""
    return set(_enabled)


def active_edition() -> str:
    """Best-guess edition label from the features that are active.

    Enterprise if any Enterprise feature is on, else Pro if any feature is on,
    else Community. Purely informational (About page, upgrade prompts).
    """
    if any(FEATURE_EDITIONS.get(f) == ENTERPRISE for f in _enabled):
        return ENTERPRISE
    if _enabled:
        return PRO
    return COMMUNITY


def clear() -> None:
    """Reset registered features — test helper, not for production use."""
    _enabled.clear()
