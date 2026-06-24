"""The wiring an edition app performs at startup.

Kept as a plain function (rather than inline in AppConfig.ready) so it is
directly unit-testable without bringing the whole app into INSTALLED_APPS.
A real Pro/Enterprise app would do exactly this from its ready().
"""

from __future__ import annotations

import logging

from vigil import editions, hooks

logger = logging.getLogger("vigil.example_extension")

# A marker the test can assert against — proves the host_approved hook fired.
approvals_seen: list[str] = []


def _on_host_approved(host=None, approved_by=None, **_extra) -> None:
    """Example handler: a real baselines app would dispatch baseline tasks here."""
    hostname = getattr(host, "hostname", str(host))
    approvals_seen.append(hostname)
    logger.info("example_extension saw host_approved for %s", hostname)


def register_extension() -> None:
    """Register this edition's features and event subscriptions with core."""
    # 1) Advertise a capability so core can light up integration points.
    editions.register_feature("ai_suggestions")
    # 2) Subscribe to a core lifecycle event.
    hooks.subscribe("host_approved", _on_host_approved)
    logger.info("example_extension registered")
