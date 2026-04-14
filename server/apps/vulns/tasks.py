import logging
from datetime import datetime, timezone

import requests
from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from apps.alerts.models import Alert, AlertRule
from apps.alerts.notifications import dispatch_alert_notification
from apps.hosts.models import Host

from .models import VulnSummary

logger = logging.getLogger(__name__)


def _get_or_create_vuln_rule():
    """Return the sentinel AlertRule for Nessus critical-vuln alerts.

    This rule has enabled=False so the metric evaluation engine ignores it;
    the sync task manages firing and resolving directly.
    """
    rule, _ = AlertRule.objects.get_or_create(
        name="Vulnerability: New Critical Findings",
        defaults={
            "category": "vulnerability",
            "metric": "critical_count",
            "operator": "gt",
            "threshold": 0,
            "severity": AlertRule.Severity.CRITICAL,
            "duration_seconds": 0,
            "enabled": False,
            "is_default": False,
        },
    )
    return rule


def _handle_vuln_alert(host, summary, prev_critical):
    """Fire or resolve a critical-vuln alert based on the change in critical count."""
    rule = _get_or_create_vuln_rule()
    existing = Alert.objects.filter(
        host=host,
        rule=rule,
        state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
    ).first()

    if summary.critical > 0 and summary.critical > prev_critical:
        if not existing:
            alert = Alert.objects.create(
                host=host,
                rule=rule,
                state=Alert.State.FIRING,
                severity=AlertRule.Severity.CRITICAL,
                message=(
                    f"Nessus: {summary.critical} critical vulnerability"
                    f"{'' if summary.critical == 1 else 's'} on {host.hostname}"
                ),
                metric_value=float(summary.critical),
            )
            logger.info("Vuln alert fired: %d criticals on %s", summary.critical, host.hostname)
            dispatch_alert_notification(alert, event="firing")

    elif summary.critical == 0 and existing and existing.state == Alert.State.FIRING:
        existing.state = Alert.State.RESOLVED
        existing.resolved_at = now()
        existing.save(update_fields=["state", "resolved_at"])
        logger.info("Vuln alert resolved: %s now has 0 criticals", host.hostname)
        dispatch_alert_notification(existing, event="resolved")


@shared_task(name="vulns.sync_nessus_vulns")
def sync_nessus_vulns():
    """Periodic task: pull vulnerability findings from Nessus and update VulnSummary records."""
    nessus_url = getattr(settings, "NESSUS_URL", "").rstrip("/")
    access_key = getattr(settings, "NESSUS_ACCESS_KEY", "")
    secret_key = getattr(settings, "NESSUS_SECRET_KEY", "")
    verify_ssl = getattr(settings, "NESSUS_VERIFY_SSL", True)

    if not all([nessus_url, access_key, secret_key]):
        return "Nessus not configured — set NESSUS_URL, NESSUS_ACCESS_KEY, NESSUS_SECRET_KEY"

    headers = {
        "X-ApiKeys": f"accessKey={access_key};secretKey={secret_key}",
        "Content-Type": "application/json",
    }

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
            except VulnSummary.DoesNotExist:
                summary = VulnSummary(host=vigil_host)
                prev_critical = 0

            summary.critical = h.get("critical", 0)
            summary.high = h.get("high", 0)
            summary.medium = h.get("medium", 0)
            summary.low = h.get("low", 0)
            summary.info = h.get("info", 0)
            summary.last_scan_at = scan_time
            summary.scanner_scan_id = scan_id
            summary.save()
            updated += 1

            _handle_vuln_alert(vigil_host, summary, prev_critical)

    return f"Synced {updated} host summaries from {len(completed)} scan(s) ({errors} errors)"
