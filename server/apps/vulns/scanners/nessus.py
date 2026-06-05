"""Nessus / Tenable scanner implementation.

This module is the existing ``apps/vulns/tasks.py`` Nessus code wrapped in
the :class:`Scanner` ABC. No behavior change — same API calls, same
parsing, same fire/resolve flow. The only structural shift is that all
state is reached via ``self`` instead of module globals, and ``sync()`` is
the single entry point the periodic Celery task calls.

Scanner identifier: ``"nessus"``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import ClassVar

import requests
from django.conf import settings
from django.utils.timezone import now

from apps.alerts.models import Alert, AlertRule
from apps.alerts.notifications import dispatch_alert_notification
from apps.hosts.models import Host

from ..models import VulnFinding, VulnScan, VulnSummary
from ..scoring import recompute_summary
from .base import Scanner

logger = logging.getLogger(__name__)


# Map Nessus scan status string -> our VulnScan.State
_NESSUS_STATUS_MAP = {
    "completed": VulnScan.State.COMPLETED,
    "imported": VulnScan.State.COMPLETED,
    "running": VulnScan.State.RUNNING,
    "pending": VulnScan.State.RUNNING,
    "processing": VulnScan.State.RUNNING,
    "aborted": VulnScan.State.ABORTED,
    "canceled": VulnScan.State.ABORTED,
    "stopped": VulnScan.State.ABORTED,
    "empty": VulnScan.State.FAILED,
}

# Nessus severity integer → our VulnFinding.Severity. Anything ≥5 is
# reserved by Nessus for credentialed-scan "critical+" findings and
# rolls up to our existing CRITICAL bucket.
_NESSUS_SEVERITY_MAP = {
    0: VulnFinding.Severity.INFO,
    1: VulnFinding.Severity.LOW,
    2: VulnFinding.Severity.MEDIUM,
    3: VulnFinding.Severity.HIGH,
    4: VulnFinding.Severity.CRITICAL,
}


# ---------------------------------------------------------------------------
# Alert helpers — shared between the critical + high count paths.
# ---------------------------------------------------------------------------

def _get_or_create_vuln_rule(severity_label: str, metric: str) -> AlertRule:
    """Return the sentinel AlertRule for vuln alerts at ``severity_label``.

    These rules have enabled=False so the metric evaluation engine ignores
    them; the sync directly fires and resolves alerts. One rule per
    severity so the operator sees distinct rows in the alert history.
    """
    if severity_label == "critical":
        name = "Vulnerability: New Critical Findings"
        severity = AlertRule.Severity.CRITICAL
    else:
        name = "Vulnerability: New High Findings"
        severity = AlertRule.Severity.WARNING
    rule, _ = AlertRule.objects.get_or_create(
        name=name,
        defaults={
            "category": "vulnerability",
            "metric": metric,
            "operator": "gt",
            "threshold": 0,
            "severity": severity,
            "duration_seconds": 0,
            "enabled": False,
            "is_default": False,
        },
    )
    return rule


def _fire_or_resolve(host, rule, severity_const, count, prev_count, label):
    """Single fire/resolve cycle shared by the critical + high paths."""
    existing = Alert.objects.filter(
        host=host,
        rule=rule,
        state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
    ).first()

    if count > 0 and count > prev_count:
        if not existing:
            alert = Alert.objects.create(
                host=host,
                rule=rule,
                state=Alert.State.FIRING,
                severity=severity_const,
                message=(
                    f"Nessus: {count} {label} vulnerability"
                    f"{'' if count == 1 else 's'} on {host.hostname}"
                ),
                metric_value=float(count),
            )
            logger.info("Vuln alert fired: %d %s on %s", count, label, host.hostname)
            dispatch_alert_notification(alert, event="firing")

    elif count == 0 and existing and existing.state == Alert.State.FIRING:
        existing.state = Alert.State.RESOLVED
        existing.resolved_at = now()
        existing.save(update_fields=["state", "resolved_at"])
        logger.info("Vuln alert resolved: %s now has 0 %ss", host.hostname, label)
        dispatch_alert_notification(existing, event="resolved")


def _handle_vuln_alert(host, summary, prev_critical, prev_high):
    """Fire / resolve critical + high vuln alerts based on counts."""
    crit_rule = _get_or_create_vuln_rule("critical", "critical_count")
    _fire_or_resolve(
        host, crit_rule, AlertRule.Severity.CRITICAL,
        summary.critical, prev_critical, "critical",
    )
    high_rule = _get_or_create_vuln_rule("high", "high_count")
    _fire_or_resolve(
        host, high_rule, AlertRule.Severity.WARNING,
        summary.high, prev_high, "high-risk",
    )


# ---------------------------------------------------------------------------
# The scanner itself
# ---------------------------------------------------------------------------

class NessusScanner(Scanner):
    """Tenable Nessus integration.

    Uses Nessus's REST API with X-ApiKeys header auth. Talks to the
    ``/server/status``, ``/editor/scan/templates``, ``/scans``, and
    ``/scans/<id>`` endpoints. Configuration comes from environment
    variables surfaced through Django settings:

      * ``NESSUS_URL`` — e.g. ``https://nessus.lan:8834``
      * ``NESSUS_ACCESS_KEY`` / ``NESSUS_SECRET_KEY`` — generated in
        Nessus UI under ``My Account → API Keys``.
      * ``NESSUS_VERIFY_SSL`` — set to ``false`` for Essentials
        installs that ship with a self-signed cert.
    """

    name: ClassVar[str] = "nessus"

    def configured(self) -> bool:
        return self._session() is not None

    def sync(self) -> str:
        sess = self._session()
        if sess is None:
            return "not configured"
        base_url, headers, verify_ssl = sess

        # Phase 1: launch any pending scan requests.
        self._launch_pending_scans(base_url, headers, verify_ssl)

        # Phase 2: poll in-flight scans so the dashboard reflects state.
        self._poll_active_scans(base_url, headers, verify_ssl)

        # Phase 3: list completed scans and ingest per-host counts.
        try:
            resp = requests.get(
                f"{base_url}/scans",
                headers=headers,
                verify=verify_ssl,
                timeout=30,
            )
            resp.raise_for_status()
            scans = resp.json().get("scans") or []
        except Exception as exc:
            logger.error("Nessus: failed to list scans: %s", exc)
            return f"error listing scans: {exc}"

        completed = [s for s in scans if s.get("status") == "completed"]
        if not completed:
            return "no completed scans found"

        updated = 0
        errors = 0

        for scan in completed:
            scan_id = scan["id"]
            try:
                detail_resp = requests.get(
                    f"{base_url}/scans/{scan_id}",
                    headers=headers,
                    verify=verify_ssl,
                    timeout=30,
                )
                detail_resp.raise_for_status()
                data = detail_resp.json()
            except Exception as exc:
                logger.warning("Nessus: scan %s detail failed: %s", scan_id, exc)
                errors += 1
                continue

            scan_info = data.get("info") or {}
            scan_end_ts = scan_info.get("scan_end")
            scan_time = (
                datetime.fromtimestamp(scan_end_ts, tz=timezone.utc)
                if scan_end_ts
                else None
            )

            for h in data.get("hosts") or []:
                host_ip = h.get("hostname", "")
                nessus_host_id = h.get("host_id")
                if not host_ip:
                    continue

                # Match to Vigil Host by IP first, then hostname substring
                vigil_host = Host.objects.filter(ip_address=host_ip).first()
                if not vigil_host:
                    vigil_host = Host.objects.filter(hostname__icontains=host_ip).first()
                if not vigil_host:
                    continue

                try:
                    summary = VulnSummary.objects.get(host=vigil_host)
                    prev_critical = summary.critical
                    prev_high = summary.high
                except VulnSummary.DoesNotExist:
                    summary = VulnSummary.objects.create(host=vigil_host)
                    prev_critical = 0
                    prev_high = 0

                # Ingest per-finding rows when we have the Nessus host_id.
                # On failure we fall back to writing the summary counts
                # directly so the dashboard still updates even if the
                # per-host endpoint hiccups.
                wrote_findings = False
                if nessus_host_id is not None:
                    wrote_findings = self._ingest_findings_for_host(
                        base_url, headers, verify_ssl,
                        scan_id, nessus_host_id, vigil_host,
                    )

                if wrote_findings:
                    # recompute_summary already wrote the recomputed
                    # counts + score; refresh local state for the alert
                    # path below.
                    summary.refresh_from_db()
                else:
                    summary.critical = h.get("critical", 0)
                    summary.high = h.get("high", 0)
                    summary.medium = h.get("medium", 0)
                    summary.low = h.get("low", 0)
                    summary.info = h.get("info", 0)

                summary.last_scan_at = scan_time
                summary.scanner_scan_id = scan_id
                summary.save(update_fields=["last_scan_at", "scanner_scan_id", "synced_at"])
                updated += 1

                _handle_vuln_alert(vigil_host, summary, prev_critical, prev_high)

        return f"synced {updated} host summaries from {len(completed)} scan(s) ({errors} errors)"

    # ----- internal helpers ------------------------------------------------

    def _session(self):
        """Return (base_url, headers, verify_ssl) or None if not configured."""
        nessus_url = getattr(settings, "NESSUS_URL", "").rstrip("/")
        access_key = getattr(settings, "NESSUS_ACCESS_KEY", "")
        secret_key = getattr(settings, "NESSUS_SECRET_KEY", "")
        verify_ssl = getattr(settings, "NESSUS_VERIFY_SSL", True)
        if not all([nessus_url, access_key, secret_key]):
            return None
        headers = {
            "X-ApiKeys": f"accessKey={access_key};secretKey={secret_key}",
            "Content-Type": "application/json",
        }
        return nessus_url, headers, verify_ssl

    def _resolve_basic_template_uuid(self, base_url, headers, verify_ssl):
        """Look up the UUID of Nessus's "Basic Network Scan" template.

        Returns the UUID string or None on failure. The UUID is stable per
        Nessus install but differs across installs, so we resolve at runtime
        rather than hardcoding.
        """
        try:
            resp = requests.get(
                f"{base_url}/editor/scan/templates",
                headers=headers, verify=verify_ssl, timeout=20,
            )
            resp.raise_for_status()
            for tpl in resp.json().get("templates") or []:
                if tpl.get("name") in ("basic", "Basic Network Scan"):
                    return tpl.get("uuid")
        except Exception as exc:
            logger.warning("Nessus: template lookup failed: %s", exc)
        return None

    def _launch_pending_scans(self, base_url, headers, verify_ssl):
        """For every Nessus VulnScan in REQUESTED state, create + launch."""
        pending = list(
            VulnScan.objects.filter(
                state=VulnScan.State.REQUESTED,
                scanner=self.name,
            ).select_related("host")
        )
        if not pending:
            return

        template_uuid = self._resolve_basic_template_uuid(base_url, headers, verify_ssl)
        if not template_uuid:
            for scan in pending:
                scan.state = VulnScan.State.FAILED
                scan.error = "Could not resolve Basic Network Scan template UUID from Nessus"
                scan.finished_at = now()
                scan.save(update_fields=["state", "error", "finished_at"])
            return

        for scan in pending:
            target = (scan.target or scan.host.ip_address or "").strip()
            if not target:
                scan.state = VulnScan.State.FAILED
                scan.error = "Host has no IP address to scan"
                scan.finished_at = now()
                scan.save(update_fields=["state", "error", "finished_at"])
                continue
            payload = {
                "uuid": template_uuid,
                "settings": {
                    "name": f"Vigil: {scan.host.hostname}",
                    "text_targets": target,
                    "description": f"Auto-launched by Vigil for host {scan.host.hostname}",
                },
            }
            try:
                r = requests.post(
                    f"{base_url}/scans",
                    headers=headers, verify=verify_ssl, timeout=20,
                    json=payload,
                )
                r.raise_for_status()
                nessus_scan_id = (r.json().get("scan") or {}).get("id")
                if not nessus_scan_id:
                    raise RuntimeError(f"Nessus returned no scan id: {r.text[:200]}")

                r2 = requests.post(
                    f"{base_url}/scans/{nessus_scan_id}/launch",
                    headers=headers, verify=verify_ssl, timeout=20,
                )
                r2.raise_for_status()

                scan.state = VulnScan.State.LAUNCHED
                scan.external_scan_id = str(nessus_scan_id)
                scan.launched_at = now()
                scan.save(update_fields=["state", "external_scan_id", "launched_at"])
                logger.info("Launched Nessus scan %s for %s", nessus_scan_id, scan.host.hostname)
            except Exception as exc:
                scan.state = VulnScan.State.FAILED
                scan.error = f"Nessus launch failed: {exc}"[:1000]
                scan.finished_at = now()
                scan.save(update_fields=["state", "error", "finished_at"])
                logger.warning("Failed to launch Nessus scan for %s: %s", scan.host.hostname, exc)

    def _ingest_findings_for_host(
        self, base_url, headers, verify_ssl,
        scan_id, nessus_host_id, vigil_host,
    ) -> bool:
        """Pull per-plugin vuln rows for one host and reconcile findings.

        Returns True on success so the caller knows the summary's
        ``critical``/``high``/``medium``/``low``/``info`` counts came
        from the findings table (via :func:`recompute_summary`) rather
        than the scan's aggregate fields.

        Reconciliation:
          * Bump ``last_seen`` (auto via ``auto_now``) on existing rows
            whose plugin still appears.
          * Create new rows for plugins not seen before.
          * Mark any previously-OPEN finding for this (host, scanner)
            that did NOT appear in this response as ``FIXED``.

        Errors are logged and the caller falls back to the aggregate
        path — we never want a flaky per-host call to skip a sync.
        """
        try:
            r = requests.get(
                f"{base_url}/scans/{scan_id}/hosts/{nessus_host_id}",
                headers=headers, verify=verify_ssl, timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            logger.warning(
                "Nessus: per-host detail for scan=%s host=%s failed: %s",
                scan_id, nessus_host_id, exc,
            )
            return False

        vulnerabilities = payload.get("vulnerabilities") or []
        seen_plugin_ids: set[str] = set()

        for vuln in vulnerabilities:
            plugin_id = vuln.get("plugin_id")
            if plugin_id is None:
                continue
            plugin_key = str(plugin_id)
            seen_plugin_ids.add(plugin_key)

            severity_int = vuln.get("severity", 0)
            severity = _NESSUS_SEVERITY_MAP.get(severity_int, VulnFinding.Severity.INFO)
            title = (vuln.get("plugin_name") or "")[:255]

            # update_or_create resets state=OPEN if a previously-FIXED
            # finding has come back, which is what we want — a plugin
            # reappearing is a regression, not a brand-new finding.
            VulnFinding.objects.update_or_create(
                host=vigil_host,
                scanner=VulnScan.Scanner.NESSUS,
                plugin_id_or_oid=plugin_key,
                defaults={
                    "severity": severity,
                    "title": title,
                    "state": VulnFinding.State.OPEN,
                    "resolved_at": None,
                },
            )

        # Anything previously OPEN for this (host, scanner) that didn't
        # show up this round has been fixed.
        stale = VulnFinding.objects.filter(
            host=vigil_host,
            scanner=VulnScan.Scanner.NESSUS,
            state=VulnFinding.State.OPEN,
        ).exclude(plugin_id_or_oid__in=seen_plugin_ids)
        if stale.exists():
            stale.update(state=VulnFinding.State.FIXED, resolved_at=now())

        recompute_summary(vigil_host)
        return True

    def _poll_active_scans(self, base_url, headers, verify_ssl):
        """Update local state for Nessus VulnScans whose jobs are in flight."""
        active = VulnScan.objects.filter(
            state__in=[VulnScan.State.LAUNCHED, VulnScan.State.RUNNING],
            external_scan_id__isnull=False,
            scanner=self.name,
        )
        for scan in active:
            try:
                r = requests.get(
                    f"{base_url}/scans/{scan.external_scan_id}",
                    headers=headers, verify=verify_ssl, timeout=20,
                )
                r.raise_for_status()
                info = (r.json().get("info") or {})
                status = (info.get("status") or "").lower()
                mapped = _NESSUS_STATUS_MAP.get(status, VulnScan.State.RUNNING)
                if mapped != scan.state:
                    scan.state = mapped
                    if mapped in {
                        VulnScan.State.COMPLETED,
                        VulnScan.State.FAILED,
                        VulnScan.State.ABORTED,
                    }:
                        scan.finished_at = now()
                    scan.save(update_fields=["state", "finished_at"])
            except Exception as exc:
                logger.warning("Nessus: poll for scan %s failed: %s", scan.external_scan_id, exc)
