"""Feature gating — thin façade over :mod:`vigil.licensing`.

**There is no Pro tier and no Enterprise tier.** Vigil 2026.4.0 folded the
never-shipped Pro edition into Free; the only paid tier is **Business**,
unlocked by a signed license (see ``vigil/licensing.py`` and
``SQSY-LICENSING.md``). The old separate-repo edition model
(``Vigil-Pro`` / ``Vigil-Enterprise``) is gone — Business code ships in this
repo under ``server/apps_business/`` and lights up at runtime.

This module keeps the two names core templates and extensions already use:

- :func:`feature_enabled` — the gate. Free features are always on; Business
  features consult the license; extension-registered features are on when
  the extension registered them.
- :func:`register_feature` — still the way an operator-installed extra app
  (``VIGIL_EXTRA_APPS``) advertises a capability so core lights up its
  integration points. This is a UX/wiring switch, not license enforcement.
"""

from __future__ import annotations

import logging

from vigil import licensing

logger = logging.getLogger("vigil.editions")

FREE = "free"
BUSINESS = "business"

#: Which tier introduces each optional feature. Drives docs and upgrade
#: prompts. Keep in sync with docs/EDITIONS.md.
FEATURE_TIERS: dict[str, str] = (
    {f: FREE for f in licensing.FREE_FEATURES}
    | {f: BUSINESS for f in licensing.BUSINESS_FEATURES}
)

_registered: set[str] = set()


def register_feature(name: str) -> None:
    """An extra app advertising a capability (UX wiring, not licensing)."""
    if name not in FEATURE_TIERS:
        logger.warning("Registering unknown feature %r", name)
    _registered.add(name)
    logger.info("Extension feature enabled: %s", name)


def feature_enabled(name: str) -> bool:
    """True if *name* is free, licensed, or extension-registered."""
    return name in _registered or licensing.has_feature(name)


def enabled_features() -> set[str]:
    """Every currently-active optional feature (for the About/license API)."""
    active = {f for f in FEATURE_TIERS if licensing.has_feature(f)}
    return active | set(_registered)


def active_edition() -> str:
    """``free`` or ``business`` — purely informational (About page)."""
    return licensing.current_state().tier


def clear() -> None:
    """Reset extension registrations — test helper only."""
    _registered.clear()
