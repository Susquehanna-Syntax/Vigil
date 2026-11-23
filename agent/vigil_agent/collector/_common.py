"""Shared helpers and state for the Vigil metrics collector.

The collector used to be one module; these are the pieces two or more of its
per-category modules need, so they live here once — including the stdlib names
the test suite patches on the package (``collector.psutil``, ``collector.sys``,
``collector.shutil``, ``collector.subprocess``), which every category module
imports from here rather than importing itself: a patch reaches one binding, and
having it in one place is what makes that binding the one everything uses.
``logger`` keeps its ``vigil.collector`` name because operators (and tests) key
off it.
"""

import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import psutil

logger = logging.getLogger("vigil.collector")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _point(category: str, metric: str, value: float, labels: dict | None = None) -> dict:
    return {
        "category": category,
        "metric": metric,
        "value": round(value, 2),
        "labels": labels or {},
        "time": _now_iso(),
    }


_warned: set[str] = set()


def _warn_once(what: str, exc: Exception) -> None:
    """Log a sub-metric failure once per process — the cause (no PDH access for
    the monitor-mode service account) cannot change while the agent runs."""
    if what in _warned:
        return
    _warned.add(what)
    logger.warning("%s unavailable, skipping it: %s", what, exc)
