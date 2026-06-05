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

import json
import logging
from typing import ClassVar

from django.utils.timezone import now

from ..models import VulnFinding, VulnScan
from ..scoring import recompute_summary
from .base import Scanner

logger = logging.getLogger(__name__)


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

        Returns a short status string for the task-completion logger.
        """
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            logger.warning("Trivy: invalid JSON from %s: %s", host.hostname, exc)
            return f"invalid JSON: {exc}"

        seen_keys: set[str] = set()
        results = data.get("Results") or []
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

        recompute_summary(host)
        return f"trivy: {finding_count} finding(s), {fixed_count} fixed"
