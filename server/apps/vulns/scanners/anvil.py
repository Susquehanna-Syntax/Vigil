"""Anvil — import its findings (SARIF 2.1.0 + the anvil/* extension) (M10).

Anvil finds; Vigil acts. Anvil reads advisory feeds, matches, and proposes
patches it never applies; Vigil ingests its record like any other scanner's
report, so Anvil's findings land in the same finding, evidence and fix-group
model and are fixed through Vigil's signed tasks. Vigil builds no advisory
importer of its own for this: the record carries everything.

A record is validated against Anvil's own JSON Schema (copied into
``apps/vulns/data``) before a single row is touched. Then:

* findings Anvil judged ``false_positive`` are skipped;
* a finding needs a host: the one the upload names, or a logical location
  whose name is a known hostname. Repository findings (SAST, DAST, SCA)
  with neither are counted and skipped — Vigil tracks hosts;
* severity comes from the SARIF ``level`` (error → high, warning → medium,
  note → low), raised to critical when Anvil marks it a KEV member;
* the package and version come from the result's ``pkg:`` purl; Anvil's
  advisory excerpt, CVEs, EPSS and detector confidence are kept.

Reconciliation is per (host, scanner=anvil): an Anvil finding on a host that
the next record for that host no longer carries is fixed.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from django.utils.timezone import now

from ..evidence import add_evidence
from ..models import FindingEvidence, VulnFinding
from ..scoring import recompute_summary
from .trivy import ScanIngestError

_SCHEMA = Path(__file__).resolve().parents[1] / "data" / "anvil-record-v1.schema.json"
_LEVEL = {"error": "high", "warning": "medium", "note": "low", "none": "info"}
_PURL = re.compile(r"pkg:[a-z0-9.+-]+/[^\s\"'<>]+", re.IGNORECASE)


@lru_cache(maxsize=1)
def _validator():
    import jsonschema

    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def validate_record(record) -> None:
    """Raise ScanIngestError naming the first few schema violations."""
    if not isinstance(record, dict):
        raise ScanIngestError("an Anvil record must be a JSON object")
    errors = sorted(_validator().iter_errors(record), key=lambda e: list(e.absolute_path))
    if errors:
        shown = "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message[:160]}"
                          for e in errors[:5])
        raise ScanIngestError(f"not a valid Anvil record ({len(errors)} schema error(s)): {shown}")


def _purl(result: dict) -> tuple[str, str]:
    """(package name, version) from the first purl anywhere in the result."""
    match = _PURL.search(json.dumps(result.get("locations") or [])) or \
        _PURL.search(json.dumps(result))
    if not match:
        return "", ""
    purl = match.group(0).split("?", 1)[0].split("#", 1)[0]
    base, _, version = purl.partition("@")
    return base.rsplit("/", 1)[-1], version


def _hostnames(result: dict) -> set[str]:
    names = set()
    for loc in result.get("locations") or []:
        for logical in (loc or {}).get("logicalLocations") or []:
            for key in ("name", "fullyQualifiedName"):
                value = (logical or {}).get(key)
                if isinstance(value, str) and value:
                    names.add(value.lower())
    return names


def ingest_record(record, *, host=None) -> dict:
    """Validate and import one Anvil record. Returns counts."""
    from apps.hosts.models import Host

    validate_record(record)
    by_name = {h.hostname.lower(): h for h in Host.objects.exclude(status=Host.Status.REJECTED)}
    seen: dict = {}
    counts = {"imported": 0, "false_positive": 0, "no_host": 0}
    for run in record.get("runs") or []:
        for result in run.get("results") or []:
            props = result.get("properties") or {}
            if props.get("anvil/verdict") == "false_positive":
                counts["false_positive"] += 1
                continue
            target = host or next((by_name[n] for n in _hostnames(result) if n in by_name), None)
            if target is None:
                counts["no_host"] += 1
                continue
            advisory = props.get("anvil/advisory") or {}
            risk = props.get("anvil/risk") or {}
            cves = [c.upper() for c in advisory.get("cveIds") or [] if isinstance(c, str)]
            severity = _LEVEL.get(str(result.get("level") or "warning"), "medium")
            if risk.get("kevMember"):
                severity = "critical"
            package, version = _purl(result)
            excerpt = advisory.get("excerpt")
            if isinstance(excerpt, dict):   # a trustedString: {text, trust}
                excerpt = excerpt.get("text")
            finding_id = props.get("anvil/findingId") or result["partialFingerprints"]["anvilFindingId/v1"]
            key = f"anvil:{str(finding_id)[:120]}"
            finding, _ = VulnFinding.objects.update_or_create(
                host=target, scanner="anvil", plugin_id_or_oid=key,
                defaults={
                    "cve_id": cves[0] if cves else "",
                    "title": str((result.get("message") or {}).get("text") or result["ruleId"])[:255],
                    "severity": severity,
                    "package_name": package[:255],
                    "installed_version": version[:80],
                    "description": str(excerpt or props.get("anvil/reasoning") or "")[:20000],
                    "references": [],
                    "vendor_status": "",
                    "advisory": {"rule": result.get("ruleId"), "cves": cves,
                                 "advisory_ids": advisory.get("ids") or [],
                                 "confidence": props.get("anvil/confidence"),
                                 "verdict": props.get("anvil/verdict"),
                                 "evidence_class": props.get("anvil/evidenceClass"),
                                 "epss": risk.get("epssScore")},
                    "state": VulnFinding.State.OPEN, "resolved_at": None,
                })
            kind = FindingEvidence.Kind.PACKAGE if package else FindingEvidence.Kind.SCANNER
            add_evidence(finding, kind, package or key,
                         f"Anvil ({props.get('anvil/evidenceClass')}): "
                         f"{package} {version}".strip()[:300],
                         {"evidence_class": props.get("anvil/evidenceClass")})
            seen.setdefault(target, set()).add(key)
            counts["imported"] += 1
    for target, keys in seen.items():
        (VulnFinding.objects.filter(host=target, scanner="anvil", state=VulnFinding.State.OPEN)
         .exclude(plugin_id_or_oid__in=keys).update(state=VulnFinding.State.FIXED, resolved_at=now()))
        recompute_summary(target)
    return counts
