"""Check-in ingest for the software list.

Defensive by design, like the hardware inventory upsert it sits next to in
``apps.hosts.views.checkin``: a malformed software list must never break a
check-in, so every failure degrades to "nothing changed" and a log line.
"""
import logging
import re

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SoftwareItem, SoftwareSnapshot, name_key

logger = logging.getLogger(__name__)

MAX_ITEMS = 10000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_SOURCES = {value for value, _label in SoftwareItem.Source.choices}
_SCOPES = {value for value, _label in SoftwareItem.Scope.choices}

#: Field lengths, read off the model so they cannot drift from it.
_MAX = {f.name: f.max_length for f in SoftwareItem._meta.get_fields()}


def _text(value, max_length: int) -> str:
    return str(value if value is not None else "")[:max_length]


def _clean_item(raw):
    """Model-ready dict for one payload item, or None when unusable."""
    if not isinstance(raw, dict):
        return None
    source = _text(raw.get("source"), _MAX["source"])
    if source not in _SOURCES:
        return None
    package_id = _text(raw.get("id"), _MAX["package_id"])
    name = _text(raw.get("name"), _MAX["name"])
    if not package_id or not name:
        return None
    scope = _text(raw.get("scope"), _MAX["scope"])
    if scope not in _SCOPES:
        scope = SoftwareItem.Scope.MACHINE
    return {
        "source": source,
        "package_id": package_id,
        "name": name,
        "name_key": name_key(name)[: _MAX["name_key"]],
        "version": _text(raw.get("version"), _MAX["version"]),
        "latest_version": _text(raw.get("latest"), _MAX["latest_version"]),
        "scope": scope,
        "user": _text(raw.get("user"), _MAX["user"]),
        "publisher": _text(raw.get("publisher"), _MAX["publisher"]),
        "managed": bool(raw.get("managed", True)),
    }


def _clean_errors(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {_text(k, 120): _text(v, 300) for k, v in raw.items()}


def ingest_software(host, payload: dict):
    """Store the host's software list from a check-in payload.

    Returns the snapshot, or None when the payload is unusable or something
    went wrong — the caller (check-in) treats None as "no software this time".
    """
    try:
        return _ingest(host, payload)
    except Exception:
        logger.exception("software ingest failed for host %s", host.pk)
        return None


def _ingest(host, payload):
    if not isinstance(payload, dict):
        return None
    digest = payload.get("digest")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None

    snapshot, _ = SoftwareSnapshot.objects.get_or_create(host=host)
    if snapshot.digest == digest:
        # Moves received_at (auto_now) without touching any item's updated_at;
        # a save() with no changed fields can be a no-op at the SQL level.
        SoftwareSnapshot.objects.filter(pk=snapshot.pk).update(received_at=timezone.now())
        snapshot.refresh_from_db()
        return snapshot

    collected_at = parse_datetime(str(payload.get("collected_at") or ""))
    if collected_at is not None and timezone.is_naive(collected_at):
        collected_at = timezone.make_aware(collected_at)

    cleaned: dict[tuple, dict] = {}
    for raw in raw_items:
        item = _clean_item(raw)
        if item is None:
            continue
        # Last one wins within one payload.
        cleaned[(item["source"], item["package_id"], item["scope"], item["user"])] = item

    items = list(cleaned.items())[:MAX_ITEMS]
    # Only what is kept counts as present: rows past the cap are dropped too.
    wanted = {key for key, _ in items}

    with transaction.atomic():
        existing = {
            (row.source, row.package_id, row.scope, row.user): row
            for row in SoftwareItem.objects.filter(host=host)
        }
        gone = [row.pk for key, row in existing.items() if key not in wanted]
        if gone:
            SoftwareItem.objects.filter(pk__in=gone).delete()

        stamp = timezone.now()
        updates = []
        creates = []
        for key, fields in items:
            row = existing.get(key)
            if row is None:
                row = SoftwareItem(host=host, first_seen=stamp, **fields)
                row.outdated_since = stamp if row.outdated else None
                creates.append(row)
                continue
            for field, value in fields.items():
                setattr(row, field, value)
            if not row.outdated:
                row.outdated_since = None
            elif row.outdated_since is None:
                row.outdated_since = stamp
            # bulk_update skips auto_now, so the stamp is set here.
            row.updated_at = stamp
            updates.append(row)
        if updates:
            SoftwareItem.objects.bulk_update(updates, [
                "name", "name_key", "version", "latest_version",
                "scope", "user", "publisher", "managed", "updated_at",
                "outdated_since",
            ])
        SoftwareItem.objects.bulk_create(creates)
        from .updates import sync_linux_pending
        sync_linux_pending(host, stamp)

    snapshot.digest = digest
    snapshot.collected_at = collected_at
    snapshot.item_count = len(items)
    snapshot.errors = _clean_errors(payload.get("errors"))
    snapshot.save()
    _match_vulns(host)
    return snapshot


def _match_vulns(host) -> None:
    """A new inventory is a new answer to "what is vulnerable here?" (M9).
    Skipped until OSV data has been loaded; never fails the ingest."""
    from apps.vulns.models import OsvAdvisory

    if not OsvAdvisory.objects.exists():
        return
    try:
        from apps.vulns.matcher import match_host
        match_host(host)
    except Exception:  # noqa: BLE001 — the inventory is stored either way
        logger.exception("vulnerability match after inventory failed for %s", host)
