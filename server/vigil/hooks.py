"""In-process event bus — the extension seam for commercial editions.

Vigil core (Community) is self-contained. The Pro and Enterprise editions ship
as separate repos whose Django apps are loaded via ``VIGIL_EXTRA_APPS`` (see
``docs/pro-extension-points.md``). Those apps subscribe to lifecycle events
here, in their ``AppConfig.ready()``, instead of patching core code.

Core emits; editions listen. Core never imports edition code, so a missing
edition simply means nobody is subscribed and the event is a no-op.

This is deliberately not Django's signal framework: a small, explicitly
documented set of event names is the contract Pro/Enterprise builds against,
and keeping it separate means the contract can't drift as core's internal
signals change.

Handlers are isolated — one raising is logged and swallowed so it can never
break the emitter or sibling handlers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger("vigil.hooks")

# The published event surface. Subscribing to or emitting an unlisted event is
# allowed (so a newer edition can ride ahead of a core release) but warns, so
# typos surface during development.
KNOWN_EVENTS = frozenset({
    "host_approved",     # payload: host, approved_by
    "host_rejected",     # payload: host, rejected_by
    "insight_created",   # payload: insight
    "alert_fired",       # payload: alert
    "task_completed",    # payload: task
})

_subscribers: dict[str, list[Callable]] = defaultdict(list)


def subscribe(event: str, handler: Callable) -> None:
    """Register *handler* to be called on *event*. Idempotent per handler."""
    if event not in KNOWN_EVENTS:
        logger.warning("Subscribing to unknown hook event %r", event)
    if handler not in _subscribers[event]:
        _subscribers[event].append(handler)


def emit(event: str, **payload) -> None:
    """Invoke every subscriber of *event* with *payload*.

    Never raises: a failing handler is logged and the rest still run.
    """
    if event not in KNOWN_EVENTS:
        logger.warning("Emitting unknown hook event %r", event)
    for handler in list(_subscribers.get(event, ())):
        try:
            handler(**payload)
        except Exception:
            logger.exception("Hook handler %r for event %r failed", handler, event)


def subscribers(event: str) -> list[Callable]:
    """Return the handlers registered for *event* (copy)."""
    return list(_subscribers.get(event, ()))


def clear(event: str | None = None) -> None:
    """Drop subscribers — test helper, not for production use."""
    if event is None:
        _subscribers.clear()
    else:
        _subscribers.pop(event, None)
