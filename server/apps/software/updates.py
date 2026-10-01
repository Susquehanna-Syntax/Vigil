"""Pending updates per host: Windows from the agent's list, Linux from the
inventory. Both feed the fleet view by update and the compliance report."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import PendingUpdate, SoftwareItem

#: The sources that are a Linux host's own package manager — what "the
#: updates this host is missing" means there. Store apps (snap, flatpak) are
#: app policy territory, not patching.
LINUX_PRIMARY_SOURCES = ("dpkg", "rpm", "apk", "pacman")

MAX_WINDOWS_UPDATES = 500


def _replace(host, kind: str, rows: dict[str, dict], stamp) -> None:
    """Make the host's *kind* rows exactly *rows*, keeping first_seen."""
    existing = {p.key: p for p in PendingUpdate.objects.filter(host=host, kind=kind)}
    gone = [p.pk for key, p in existing.items() if key not in rows]
    if gone:
        PendingUpdate.objects.filter(pk__in=gone).delete()
    updates, creates = [], []
    for key, fields in rows.items():
        first_seen = fields.pop("first_seen", None)
        row = existing.get(key)
        if row is None:
            creates.append(PendingUpdate(host=host, kind=kind, key=key,
                                         first_seen=first_seen or stamp,
                                         last_seen=stamp, **fields))
            continue
        for field, value in fields.items():
            setattr(row, field, value)
        row.last_seen = stamp
        updates.append(row)
    if updates:
        PendingUpdate.objects.bulk_update(
            updates, ["title", "severity", "classification", "reboot_required",
                      "version", "last_seen"])
    PendingUpdate.objects.bulk_create(creates)


def _classification(categories) -> str:
    """The Windows Update classification among an update's categories (the
    rest are products), or the first category when none is recognised."""
    from apps.policies.models import WINDOWS_CLASSIFICATIONS

    names = [str(c) for c in categories or []]
    for name in names:
        if name in WINDOWS_CLASSIFICATIONS:
            return name
    return names[0][:60] if names else ""


def ingest_windows_updates(host, raw) -> bool:
    """Store the agent's per-update list. Anything but a list is ignored —
    an absent key is an old agent or no fresh scan, and the rows stay."""
    if not isinstance(raw, list):
        return False
    rows: dict[str, dict] = {}
    for item in raw[:MAX_WINDOWS_UPDATES]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("kb") or item.get("update_id") or "").strip()[:300]
        if not key:
            continue
        rows[key] = {
            "title": str(item.get("title") or "")[:300],
            "severity": str(item.get("severity") or "").lower()[:20],
            "classification": _classification(item.get("categories")),
            "reboot_required": bool(item.get("reboot_required")),
            "version": "",
        }
    with transaction.atomic():
        _replace(host, PendingUpdate.Kind.WINDOWS, rows, timezone.now())
    return True


def sync_linux_pending(host, stamp=None) -> None:
    """Mirror the host's outdated primary-manager packages as pending rows."""
    stamp = stamp or timezone.now()
    rows: dict[str, dict] = {}
    for item in SoftwareItem.objects.filter(host=host, source__in=LINUX_PRIMARY_SOURCES):
        if not item.outdated:
            continue
        rows[item.package_id] = {
            "title": item.name[:300], "severity": "", "classification": item.source,
            "reboot_required": False, "version": item.latest_version,
            "first_seen": item.outdated_since or stamp,
        }
    _replace(host, PendingUpdate.Kind.LINUX, rows, stamp)
