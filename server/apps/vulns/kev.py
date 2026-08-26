"""The CISA Known Exploited Vulnerabilities catalogue.

Vigil ships a snapshot of the catalogue in ``data/kev_catalog.json`` so a
fresh install — including an air-gapped one — has KEV data on day one without
making a single outbound request. An optional daily refresh can pull a newer
copy when ``VIGIL_KEV_LIVE_REFRESH`` is on.

Both paths funnel through :func:`upsert_entries`, so there is one parser and
one write path regardless of where the bytes came from.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BUNDLED_CATALOG = Path(__file__).resolve().parent / "data" / "kev_catalog.json"

#: The one URL the refresh will ever fetch. Deliberately a module constant and
#: NOT read from settings, the database, or the ``source`` field of the bundled
#: JSON — anything an attacker could write to becomes an SSRF pivot, and this
#: repo has already shipped two of those. If CISA moves the feed, that is a code
#: change and a code review, which is the point.
KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)

#: Refuse a response larger than this. The real catalogue is ~1.6 MB; 16 MB is
#: generous headroom while still bounding memory if the endpoint is hostile.
MAX_FEED_BYTES = 16 * 1024 * 1024


def _parse_date(value: Any):
    """Return a ``date`` for an ISO ``YYYY-MM-DD`` string, else ``None``."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_catalog(payload: dict) -> list[dict]:
    """Turn a catalogue document into a list of normalized entry dicts.

    Tolerates junk: an entry without a usable ``cve_id`` is skipped rather than
    raising, because one malformed row must not cost us the other 1675.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    for raw in payload.get("vulnerabilities") or []:
        if not isinstance(raw, dict):
            continue
        cve_id = str(raw.get("cve_id") or raw.get("cveID") or "").strip().upper()
        if not cve_id or cve_id in seen:
            continue
        seen.add(cve_id)
        entries.append({
            "cve_id": cve_id,
            "date_added": _parse_date(raw.get("date_added") or raw.get("dateAdded")),
            "cisa_due_date": _parse_date(raw.get("due_date") or raw.get("dueDate")),
            "ransomware": bool(raw.get("ransomware")),
            "name": str(raw.get("name") or raw.get("vulnerabilityName") or "")[:255],
        })
    return entries


def upsert_entries(entries: Iterable[dict], source: str = "bundled") -> int:
    """Write entries to :class:`~apps.vulns.models.KevEntry`. Idempotent."""
    from .models import KevEntry

    written = 0
    for entry in entries:
        if not entry.get("date_added"):
            # date_added is non-null on the model; an entry without one is
            # malformed upstream and not worth inventing a date for.
            continue
        KevEntry.objects.update_or_create(
            cve_id=entry["cve_id"],
            defaults={
                "date_added": entry["date_added"],
                "cisa_due_date": entry.get("cisa_due_date"),
                "ransomware": entry.get("ransomware", False),
                "name": entry.get("name", ""),
                "source": source,
            },
        )
        written += 1
    return written


def load_bundled(path: Path | None = None) -> int:
    """Load the snapshot shipped with the code. Never raises.

    Called from a data migration as well as ``manage.py load_kev``, so a broken
    or missing file must degrade to "no KEV data" rather than failing a deploy.
    Vulnerability metadata is not worth blocking a migration over.
    """
    path = path or BUNDLED_CATALOG
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        logger.warning("KEV catalogue not found at %s — continuing with none", path)
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("KEV catalogue at %s is unreadable (%s) — continuing", path, exc)
        return 0
    return upsert_entries(parse_catalog(payload), source="bundled")


def fetch_live() -> int:
    """Fetch a fresh catalogue from CISA and upsert it. Never raises.

    Every failure mode — disabled, network, status, redirect, oversize, parse —
    logs and returns 0 without touching a single row, leaving the bundled
    snapshot in place. A KEV refresh failure must never affect ingest, scoring
    or alerting.
    """
    from django.conf import settings

    if not getattr(settings, "VIGIL_KEV_LIVE_REFRESH", False):
        logger.debug("KEV live refresh is disabled; using the bundled catalogue")
        return 0

    import requests

    try:
        response = requests.get(
            KEV_FEED_URL,
            timeout=(10, 60),
            # A redirect is a failure, not something to follow. Following one
            # is how a feed URL turns into a request against link-local
            # metadata or an internal host.
            allow_redirects=False,
            stream=True,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        logger.warning("KEV refresh failed to connect (%s) — keeping bundled data", exc)
        return 0

    try:
        if response.status_code != 200:
            logger.warning(
                "KEV refresh got HTTP %s — keeping bundled data", response.status_code
            )
            return 0

        # Trust the declared length only as an early reject; count real bytes
        # as they arrive, because Content-Length can lie or be absent.
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
            logger.warning("KEV refresh response declares %s bytes — refusing", declared)
            return 0

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                logger.warning("KEV refresh exceeded %s bytes — refusing", MAX_FEED_BYTES)
                return 0
            chunks.append(chunk)

        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("KEV refresh returned unparsable data (%s)", exc)
        return 0
    except requests.RequestException as exc:
        logger.warning("KEV refresh failed mid-stream (%s)", exc)
        return 0
    finally:
        response.close()

    return upsert_entries(parse_catalog(payload), source="live")
