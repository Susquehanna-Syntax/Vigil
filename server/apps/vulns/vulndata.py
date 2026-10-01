"""Load vulnerability data — OSV records, the CISA KEV catalogue, FIRST EPSS —
into the local store (M9), from an offline bundle or straight from the source.

An offline bundle is a .zip, .tar.gz or a directory holding any of:

* ``osv/…/*.json`` — OSV records, one per file, in any folder layout; and/or
  ``osv/<anything>.zip`` — an OSV.dev per-ecosystem dump (``all.zip``);
* ``kev.json`` — the CISA catalogue as CISA publishes it;
* ``epss.csv`` or ``epss.csv.gz`` — FIRST's daily scores file.

An air-gapped install gets the same data a connected one fetches, by
carrying the files across. Nothing here needs the network.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import tarfile
import zipfile
from pathlib import Path

from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime

from .cvss import cvss3_base_score, severity_for_score
from .models import EpssScore, OsvAdvisory, OsvAffected

logger = logging.getLogger(__name__)

#: OSV.dev's public bucket — one all.zip per ecosystem.
OSV_BUCKET = "https://osv-vulnerabilities.storage.googleapis.com/{ecosystem}/all.zip"
EPSS_URL = "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz"

_DB_SEVERITY = {"critical": "critical", "high": "high", "important": "high",
                "moderate": "medium", "medium": "medium", "low": "low",
                "negligible": "low", "unimportant": "low"}


def _severity(record: dict) -> tuple[str, float | None, str]:
    vector = ""
    for entry in record.get("severity") or []:
        if isinstance(entry, dict) and str(entry.get("type", "")).startswith("CVSS_V3"):
            vector = str(entry.get("score") or "")[:200]
            break
    score = cvss3_base_score(vector) if vector else None
    if score is not None:
        return severity_for_score(score), score, vector
    rated = ""
    for holder in [record.get("database_specific") or {}] + [
            (a.get("ecosystem_specific") or {}) for a in record.get("affected") or []
            if isinstance(a, dict)]:
        value = holder.get("severity") if isinstance(holder, dict) else None
        if isinstance(value, str) and value.lower() in _DB_SEVERITY:
            rated = _DB_SEVERITY[value.lower()]
            break
    return rated, None, vector


def _first_fixed(ranges) -> str:
    fixed = [str(e["fixed"]) for r in ranges or [] if isinstance(r, dict)
             for e in r.get("events") or [] if isinstance(e, dict) and e.get("fixed")]
    return min(fixed, key=len) if fixed else ""


def import_osv_records(records) -> int:
    """Upsert OSV records; an advisory's affected rows are replaced whole."""
    count = 0
    for record in records:
        if not isinstance(record, dict) or not record.get("id"):
            continue
        severity, score, vector = _severity(record)
        refs = [r.get("url") for r in record.get("references") or []
                if isinstance(r, dict) and isinstance(r.get("url"), str)][:50]
        with transaction.atomic():
            adv, _ = OsvAdvisory.objects.update_or_create(
                id=str(record["id"])[:128],
                defaults={
                    "modified": parse_datetime(str(record.get("modified") or "")),
                    "summary": str(record.get("summary") or "")[:500],
                    "details": str(record.get("details") or "")[:20000],
                    "aliases": [str(a) for a in record.get("aliases") or []][:50],
                    "severity": severity, "cvss_score": score, "cvss_vector": vector,
                    "references": refs,
                    "withdrawn": bool(record.get("withdrawn")),
                })
            adv.affected.all().delete()
            rows = []
            for a in record.get("affected") or []:
                pkg = (a or {}).get("package") or {}
                eco, name = str(pkg.get("ecosystem") or ""), str(pkg.get("name") or "")
                if not eco or not name:
                    continue
                ranges = [r for r in a.get("ranges") or [] if isinstance(r, dict)]
                rows.append(OsvAffected(
                    advisory=adv, ecosystem=eco[:80], package=name[:200], ranges=ranges,
                    versions=[str(v) for v in a.get("versions") or []][:2000],
                    fixed=_first_fixed(ranges)[:120]))
            OsvAffected.objects.bulk_create(rows)
        count += 1
    return count


def _records_from_zip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                try:
                    yield json.loads(zf.read(name))
                except ValueError:
                    logger.warning("skipping unreadable OSV record %s", name)


def import_epss_csv(text: str) -> int:
    rows = [line for line in text.splitlines() if line and not line.startswith("#")]
    reader = csv.DictReader(rows)
    scores = []
    stamp = None
    for comment in (line for line in text.splitlines()[:2] if line.startswith("#")):
        for part in comment.lstrip("#").split(","):
            if part.startswith("score_date:"):
                stamp = parse_date(part.split(":", 1)[1][:10])
    for row in reader:
        cve = str(row.get("cve") or "").upper()
        try:
            scores.append(EpssScore(cve_id=cve, epss=float(row["epss"]),
                                    percentile=float(row["percentile"]), score_date=stamp))
        except (KeyError, TypeError, ValueError):
            continue
    with transaction.atomic():
        EpssScore.objects.filter(cve_id__in=[s.cve_id for s in scores]).delete()
        EpssScore.objects.bulk_create(scores, batch_size=5000)
    return len(scores)


def _members(path: Path):
    """(name, bytes) for every file in a bundle — zip, tarball or directory."""
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(path)), p.read_bytes()
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    yield name, zf.read(name)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                if member.isfile():
                    yield member.name, tf.extractfile(member).read()
    else:
        raise ValueError(f"{path.name} is not a zip, a tarball or a directory")


def import_bundle(path) -> dict:
    """Load every OSV, KEV and EPSS file in the bundle at *path*."""
    from .kev import parse_catalog, upsert_entries

    counts = {"osv": 0, "kev": 0, "epss": 0}
    for name, data in _members(Path(path)):
        base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        parts = name.replace("\\", "/").lower().split("/")
        if "osv" in parts[:-1] or base.startswith("osv"):
            if base.endswith(".zip"):
                counts["osv"] += import_osv_records(_records_from_zip(data))
            elif base.endswith(".json"):
                counts["osv"] += import_osv_records([json.loads(data)])
        elif base == "kev.json" or base.startswith("known_exploited"):
            counts["kev"] += upsert_entries(parse_catalog(json.loads(data)), source="live")
        elif base.startswith("epss") and base.endswith((".csv", ".csv.gz")):
            text = gzip.decompress(data).decode() if base.endswith(".gz") else data.decode()
            counts["epss"] += import_epss_csv(text)
    return counts


def fetch_osv(ecosystem: str, *, session=None) -> int:
    """Download and load one ecosystem's OSV dump. Needs the network."""
    import requests

    resp = (session or requests).get(OSV_BUCKET.format(ecosystem=ecosystem), timeout=300)
    resp.raise_for_status()
    return import_osv_records(_records_from_zip(resp.content))
