"""Trivy (Aqua) — agent-local vulnerability scanner.

Trivy doesn't fit the launch/poll model the way Nessus or Greenbone do.
The scan IS the agent task: each ``run_trivy_scan`` action runs Trivy
locally, captures its JSON output, and the server's task-completion
handler hands that JSON to :meth:`TrivyScanner.ingest_report`. There's
no central scanner to poll, no API to ask for the latest results — when
the agent reports the task as completed, the findings have arrived.

That means :meth:`sync` is a no-op for this scanner. We still register
it in :data:`apps.vulns.scanners.SCANNER_REGISTRY` so the rest of the
system (UI scanner badges, fleet aggregations, the no-op periodic walk)
behaves uniformly across scanners.

Trivy's JSON shape (from ``trivy fs --format json``)::

    {
      "ArtifactName": "/",
      "Results": [
        {
          "Target": "...",
          "Vulnerabilities": [
            {
              "VulnerabilityID": "CVE-2026-0001",
              "PkgName": "openssl",
              "InstalledVersion": "1.1.1",
              "FixedVersion": "3.0.0",
              "Severity": "CRITICAL",
              "Title": "OpenSSL heap buffer overflow"
            },
            ...
          ]
        },
        ...
      ]
    }

Each ``(PkgName, VulnerabilityID)`` becomes a :class:`VulnFinding` row.
Trivy's findings are always CVE-centric so ``cve_id`` and
``plugin_id_or_oid`` carry the same value — keeps the model layer
consistent without forking the dedup logic.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import re
from typing import ClassVar

from django.utils.timezone import now

from ..models import VulnFinding, VulnScan
from ..scoring import recompute_summary
from .base import Scanner

logger = logging.getLogger(__name__)


class ScanIngestError(Exception):
    """A scan report could not be trusted, so nothing was written.

    Raised before any row is touched. The alternative — ingesting a report
    we do not understand — silently marks existing findings as fixed and
    presents an unscanned host as a clean one.
    """


#: What vigil_agent.client._cap_output writes when its ceiling bites. Its
#: presence is proof the output was cut, not merely evidence.
_AGENT_TRUNCATION_MARKER = "[OUTPUT TRUNCATED"

#: Prefix the agent puts in front of a gzipped, base64-encoded report
#: (vigil_agent.executor.TRIVY_GZIP_MARKER). Absent means plain JSON, which is
#: what agents before compression send — both must keep working.
_GZIP_MARKER = "[TRIVY-GZ]"

#: base64's alphabet. Used to find where the blob ends, since a multi-step
#: transcript can carry other steps' output after it.
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]+")


def _unpack_report(text: str) -> str:
    """Decompress the report if the agent gzipped it, else return it as-is.

    A Trivy report is the same handful of keys repeated once per finding, so it
    gzips about 6.6x — 5.0x once base64 has taken its third back. That is what
    lets a host with thousands of findings arrive in one request instead of
    forcing the request-body ceiling up.

    Decompression happens before anything else looks at the text, so every
    diagnostic downstream quotes readable JSON rather than base64.
    """
    marker = text.find(_GZIP_MARKER)
    if marker == -1:
        return text

    blob = _B64_RUN.match(text, marker + len(_GZIP_MARKER))
    if blob is None:
        raise ScanIngestError(
            "The Trivy report is marked as compressed but carries no payload "
            "after the marker. Nothing was ingested and existing findings were "
            "left untouched.")

    try:
        report = gzip.decompress(base64.b64decode(blob.group(0))).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        # Overwhelmingly this means the blob was cut off in transit: a
        # truncated gzip stream cannot be decompressed at all, whereas
        # truncated plain JSON at least parses partway.
        raise ScanIngestError(
            f"The compressed Trivy report could not be decompressed "
            f"({exc}), which almost always means it was truncated in "
            f"transit. {len(blob.group(0)):,} characters of payload arrived. "
            f"Nothing was ingested and existing findings were left untouched."
        ) from exc

    # Put the decompressed report back where the blob was, so any surrounding
    # step output in a multi-step transcript survives for diagnosis.
    return text[:marker] + report + text[blob.end():]


def _extract_report(raw_output: str) -> dict:
    """Pull the Trivy JSON object out of a task's output.

    A single-step task returns the report alone. A multi-step task returns
    the runtime's transcript instead::

        [OK] refresh_db: ok
        [OK] scan: {"SchemaVersion": 2, ...}

    Feeding that to json.loads fails on the leading bracket — it is read as
    the start of a JSON array — with "Expecting value: line 1 column 2".
    That is precisely what happened to every multi-step Trivy scan ever run:
    the old code returned the parse error as a string that only reached the
    worker log, so nothing was ingested and nothing said so.

    Scans the output for the first balanced JSON object that looks like a
    Trivy report, so both shapes work.
    """
    text = (raw_output or "").strip()
    if not text:
        raise ScanIngestError("Trivy step produced no output at all.")

    # Compressed reports become plain JSON here, before any parsing or any
    # diagnostic quotes the text back at the operator.
    text = _unpack_report(text)

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            candidate, _end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(candidate, dict) and (
                "Results" in candidate or "SchemaVersion" in candidate):
            return candidate
        start = text.find("{", start + 1)

    preview = text[:200].replace("\n", " ")

    # Nothing decoded. Distinguish "the report was cut off on the way here"
    # from "there was never a report in this output" — the fixes have nothing
    # in common, and for the life of the feature the first was being reported
    # as the second. This is what was actually happening: the agent capped
    # task output at 10,000 characters and a real `trivy fs /` report is
    # ~30 MB, so what arrived was always an unterminated fragment.
    if _AGENT_TRUNCATION_MARKER in text or any(
            marker in text for marker in ('"SchemaVersion"', '"Results"')):
        raise ScanIngestError(
            f"The Trivy report is truncated — it starts but never finishes, "
            f"so no finding could be read out of it and existing findings "
            f"were left untouched. {len(text):,} characters arrived. A full "
            f"filesystem report runs to tens of megabytes, so this is a "
            f"transport limit rather than a bad scan: check that the agent on "
            f"this host is new enough to condense reports before sending them "
            f"(2026.6.2 and later), and that nothing between the agent and "
            f"this server caps request bodies. First 200 characters were: "
            f"{preview!r}")

    raise ScanIngestError(
        f"No Trivy JSON report found in the step output. First 200 "
        f"characters were: {preview!r}")


# Trivy uses upper-case severity strings — map to our enum values.
_TRIVY_SEVERITY_MAP = {
    "CRITICAL": VulnFinding.Severity.CRITICAL,
    "HIGH": VulnFinding.Severity.HIGH,
    "MEDIUM": VulnFinding.Severity.MEDIUM,
    "LOW": VulnFinding.Severity.LOW,
    "UNKNOWN": VulnFinding.Severity.INFO,
    "NONE": VulnFinding.Severity.INFO,
}


class TrivyScanner(Scanner):
    name: ClassVar[str] = "trivy"

    def configured(self) -> bool:
        # Trivy is agent-side — there's nothing to configure on the
        # server. Always "configured" so the fleet-wide score endpoint
        # treats Trivy-only fleets correctly (host has findings even
        # though sync_vulns has nothing to do).
        return True

    def sync(self) -> str:
        return "agent-driven — no server-side sync needed"

    def ingest_report(self, host, raw_output: str) -> str:
        """Parse one Trivy JSON report and reconcile :class:`VulnFinding` rows.

        Called from the task-completion handler in apps/tasks/views.py
        when a ``run_trivy_scan`` action lands. Same reconciliation
        rules as the Nessus path:

          * existing OPEN findings whose CVE still appears bump
            ``last_seen`` (auto via ``auto_now``),
          * new CVEs become new rows,
          * any OPEN finding for this (host, scanner) that didn't
            appear in this report is marked ``FIXED``.

        Raises :class:`ScanIngestError` on any report we cannot trust,
        *before* touching a single row. That distinction is the whole point:
        a report with no ``Vulnerabilities`` section is not a clean host, it
        is a scan that never looked for vulnerabilities. Treating the two
        alike marked every existing finding FIXED and reported success —
        destroying real findings while showing a green badge.

        Returns a status string with the counts, surfaced in run details.
        """
        data = _extract_report(raw_output)

        results = data.get("Results") or []
        if not results:
            raise ScanIngestError(
                "Trivy report contained no Results — nothing was scanned.")

        # An empty Vulnerabilities list means "clean". An absent key means the
        # scan did not run the vulnerability scanner at all (an SBOM, or
        # --scanners set to something else). Only the former is ingestable.
        if not any("Vulnerabilities" in r for r in results if isinstance(r, dict)):
            # Say what actually arrived: the usual cause is an agent older
            # than 2026.2.6, which invoked Trivy without --scanners vuln.
            version = ((data.get("Trivy") or {}).get("Version") or "unknown")
            keys = sorted({k for r in results if isinstance(r, dict) for k in r})
            raise ScanIngestError(
                f"Trivy report carries no Vulnerabilities section, so this "
                f"scan is inconclusive — it may be a genuinely clean host "
                f"whose section Trivy omitted, or a scan that never ran the "
                f"vulnerability scanner. Trivy version {version}; result keys "
                f"present: {', '.join(keys) or 'none'}. Existing findings for "
                f"this host were left untouched: clearing them on an "
                f"ambiguous report would turn a failed scan into a clean "
                f"bill of health.")

        seen_keys: set[str] = set()
        finding_count = 0

        for result in results:
            for vuln in result.get("Vulnerabilities") or []:
                cve_id = (vuln.get("VulnerabilityID") or "").strip()
                pkg = (vuln.get("PkgName") or "").strip()
                if not cve_id or not pkg:
                    continue
                severity_str = (vuln.get("Severity") or "UNKNOWN").upper()
                severity = _TRIVY_SEVERITY_MAP.get(severity_str, VulnFinding.Severity.INFO)

                # Plugin key is "<pkg>:<cve>" so the same CVE affecting
                # two distinct packages on one host stays as two rows.
                # That's correct — a CVE in openssl and a CVE in
                # libxml2 are separately fixable.
                plugin_key = f"{pkg}:{cve_id}"
                seen_keys.add(plugin_key)

                VulnFinding.objects.update_or_create(
                    host=host,
                    scanner=VulnScan.Scanner.TRIVY,
                    plugin_id_or_oid=plugin_key,
                    defaults={
                        "severity": severity,
                        "cve_id": cve_id,
                        "title": (vuln.get("Title") or "")[:255],
                        "package_name": pkg[:255],
                        "installed_version": (vuln.get("InstalledVersion") or "")[:80],
                        "fixed_version": (vuln.get("FixedVersion") or "")[:80],
                        "state": VulnFinding.State.OPEN,
                        "resolved_at": None,
                    },
                )
                finding_count += 1

        # Anything previously open for this host+scanner that didn't
        # reappear is fixed.
        stale = VulnFinding.objects.filter(
            host=host,
            scanner=VulnScan.Scanner.TRIVY,
            state=VulnFinding.State.OPEN,
        ).exclude(plugin_id_or_oid__in=seen_keys)
        fixed_count = stale.count()
        if fixed_count:
            stale.update(state=VulnFinding.State.FIXED, resolved_at=now())

        summary = recompute_summary(host)
        # Only a report we actually trusted sets this. Without it a
        # Trivy-scanned host reads as never-scanned forever, which is how a
        # clean host and an unscanned one came to look identical — previously
        # only the Nessus path ever wrote it.
        summary.last_scan_at = now()
        summary.save(update_fields=["last_scan_at"])

        return (f"trivy: {finding_count} finding(s) ingested, "
                f"{fixed_count} marked fixed")
