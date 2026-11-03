"""Pinned task versions (S1 decisions-06 item 16).

A task definition belongs to whoever wrote it, not to a site. When an
automation or playbook uses someone's task, a later edit to that task would
otherwise change what the automation runs, on every host it reaches, without
anyone who owns the automation agreeing. So a reference keeps a snapshot of
the version it was given; dispatch uses the snapshot unless the reference
opts into auto-sync, and the editor shows a warning with "Sync latest" when
the task has changed since.
"""
from __future__ import annotations

import hashlib

from django.utils.timezone import now

#: What a dispatch reads off a definition, and so what a snapshot keeps.
_FIELDS = ("name", "yaml_source", "parsed_spec", "risk_level")


def version_hash(definition) -> str:
    return hashlib.sha256((definition.yaml_source or "").encode()).hexdigest()


def snapshot(definition) -> dict | None:
    if definition is None:
        return None
    snap = {field: getattr(definition, field) for field in _FIELDS}
    snap["hash"] = version_hash(definition)
    snap["captured_at"] = now().isoformat()
    return snap


def is_stale(definition, snap) -> bool:
    """The task has changed since *snap* was taken."""
    return definition is not None and bool(snap) and snap.get("hash") != version_hash(definition)


def effective(definition, snap, auto_sync: bool):
    """The definition a dispatch should use: the live one with auto-sync or
    no snapshot, else an unsaved copy carrying the pinned version. The copy
    keeps the live row's id and owner, so audit and history still point at
    the real task."""
    if definition is None or auto_sync or not snap:
        return definition
    from copy import copy
    pinned = copy(definition)
    for field in _FIELDS:
        if field in snap:
            setattr(pinned, field, snap[field])
    pinned._pinned = True
    return pinned
