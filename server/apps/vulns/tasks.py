import logging
from datetime import datetime, timezone

import requests
from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from apps.alerts.models import Alert, AlertRule
from apps.alerts.notifications import dispatch_alert_notification
from apps.hosts.models import Host

from .models import VulnScan, VulnSummary

logger = logging.getLogger(__name__)


def _get_or_create_vuln_rule(severity_label: str, metric: str):
    """Return the sentinel AlertRule for Nessus vuln alerts at ``severity_label``.

    These rules have enabled=False so the metric evaluation engine ignores
    them; the sync task fires and resolves directly. One rule per severity
    so the operator sees distinct rows in the alert history.
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


def _nessus_session():
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


def _resolve_basic_template_uuid(base_url, headers, verify_ssl):
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


def _launch_pending_vuln_scans(base_url, headers, verify_ssl):
    """For every VulnScan in REQUESTED state, create + launch in Nessus."""
    pending = list(VulnScan.objects.filter(state=VulnScan.State.REQUESTED).select_related("host"))
    if not pending:
        return

    template_uuid = _resolve_basic_template_uuid(base_url, headers, verify_ssl)
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
            scan.nessus_scan_id = nessus_scan_id
            scan.launched_at = now()
            scan.save(update_fields=["state", "nessus_scan_id", "launched_at"])
            logger.info("Launched Nessus scan %s for %s", nessus_scan_id, scan.host.hostname)
        except Exception as exc:
            scan.state = VulnScan.State.FAILED
            scan.error = f"Nessus launch failed: {exc}"[:1000]
            scan.finished_at = now()
            scan.save(update_fields=["state", "error", "finished_at"])
            logger.warning("Failed to launch Nessus scan for %s: %s", scan.host.hostname, exc)


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


def _poll_active_vuln_scans(base_url, headers, verify_ssl):
    """Update local state for VulnScans whose Nessus jobs are in flight."""
    active = VulnScan.objects.filter(
        state__in=[VulnScan.State.LAUNCHED, VulnScan.State.RUNNING],
        nessus_scan_id__isnull=False,
    )
    for scan in active:
        try:
            r = requests.get(
                f"{base_url}/scans/{scan.nessus_scan_id}",
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
            logger.warning("Nessus: poll for scan %s failed: %s", scan.nessus_scan_id, exc)


@shared_task(name="vulns.sync_nessus_vulns")
def sync_nessus_vulns():
    """Periodic task: launch pending scans, poll in-flight, then ingest results."""
    sess = _nessus_session()
    if sess is None:
        return "Nessus not configured — set NESSUS_URL, NESSUS_ACCESS_KEY, NESSUS_SECRET_KEY"
    nessus_url, headers, verify_ssl = sess

    # Phase 1: launch any pending scan requests.
    _launch_pending_vuln_scans(nessus_url, headers, verify_ssl)

    # Phase 2: poll in-flight scans so the dashboard reflects state.
    _poll_active_vuln_scans(nessus_url, headers, verify_ssl)

    # Fetch scan list
    try:
        resp = requests.get(
            f"{nessus_url}/scans",
            headers=headers,
            verify=verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        scans = resp.json().get("scans") or []
    except Exception as exc:
        logger.error("Nessus: failed to list scans: %s", exc)
        return f"Error listing scans: {exc}"

    completed = [s for s in scans if s.get("status") == "completed"]
    if not completed:
        return "No completed scans found"

    updated = 0
    errors = 0

    for scan in completed:
        scan_id = scan["id"]
        try:
            detail_resp = requests.get(
                f"{nessus_url}/scans/{scan_id}",
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
                summary = VulnSummary(host=vigil_host)
                prev_critical = 0
                prev_high = 0

            summary.critical = h.get("critical", 0)
            summary.high = h.get("high", 0)
            summary.medium = h.get("medium", 0)
            summary.low = h.get("low", 0)
            summary.info = h.get("info", 0)
            summary.last_scan_at = scan_time
            summary.scanner_scan_id = scan_id
            summary.save()
            updated += 1

            _handle_vuln_alert(vigil_host, summary, prev_critical, prev_high)

    return f"Synced {updated} host summaries from {len(completed)} scan(s) ({errors} errors)"
