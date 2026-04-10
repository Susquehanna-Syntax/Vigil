import logging
import operator as op
from datetime import timedelta

from celery import shared_task
from django.db.models import Avg
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint

from .models import Alert, AlertRule
from .notifications import dispatch_alert_notification

logger = logging.getLogger(__name__)

OPERATOR_MAP = {
    "gt": op.gt,
    "lt": op.lt,
    "gte": op.ge,
    "lte": op.le,
    "eq": op.eq,
}


def _check_rule(rule, host, current_time):
    """Evaluate a single AlertRule against a host's recent metrics.

    Returns (is_breaching, latest_value) tuple.
    """
    compare = OPERATOR_MAP.get(rule.operator)
    if compare is None:
        return False, None

    # Look back over the rule's duration window (minimum 60s for a meaningful sample)
    window = max(rule.duration_seconds, 60)
    since = current_time - timedelta(seconds=window)

    points = MetricPoint.objects.filter(
        host=host,
        category=rule.category,
        metric=rule.metric,
        time__gte=since,
    ).order_by("-time")

    if not points.exists():
        return False, None

    latest_value = points.first().value

    if rule.duration_seconds == 0:
        # Instantaneous check — just the latest point
        return compare(latest_value, rule.threshold), latest_value

    # Sustained check — average over the duration window must breach
    avg = points.aggregate(avg=Avg("value"))["avg"]
    if avg is None:
        return False, None

    return compare(avg, rule.threshold), latest_value


@shared_task(name="alerts.evaluate_alert_rules")
def evaluate_alert_rules():
    """Periodic task: evaluate all enabled AlertRules against online hosts."""
    rules = AlertRule.objects.filter(enabled=True)
    if not rules.exists():
        return "No enabled rules"

    online_hosts = Host.objects.filter(status=Host.Status.ONLINE)
    if not online_hosts.exists():
        return "No online hosts"

    current_time = now()
    fired = 0
    resolved = 0

    for rule in rules:
        for host in online_hosts:
            is_breaching, latest_value = _check_rule(rule, host, current_time)

            # Check for existing firing/acknowledged alert for this rule+host
            existing = Alert.objects.filter(
                rule=rule,
                host=host,
                state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
            ).first()

            if is_breaching and not existing:
                # Fire new alert
                alert = Alert.objects.create(
                    host=host,
                    rule=rule,
                    state=Alert.State.FIRING,
                    severity=rule.severity,
                    message=f"{rule.name}: {rule.category}/{rule.metric} is {latest_value:.2f} (threshold: {rule.operator} {rule.threshold})",
                    metric_value=latest_value,
                )
                fired += 1
                logger.info("Alert fired: %s on %s (value=%.2f)", rule.name, host.hostname, latest_value)
                dispatch_alert_notification(alert, event="firing")

            elif not is_breaching and existing and existing.state == Alert.State.FIRING:
                # Auto-resolve — metric recovered
                existing.state = Alert.State.RESOLVED
                existing.resolved_at = current_time
                existing.save(update_fields=["state", "resolved_at"])
                resolved += 1
                logger.info("Alert resolved: %s on %s", rule.name, host.hostname)
                dispatch_alert_notification(existing, event="resolved")

    return f"Evaluated {rules.count()} rules × {online_hosts.count()} hosts: {fired} fired, {resolved} resolved"


@shared_task(name="alerts.mark_stale_hosts_offline")
def mark_stale_hosts_offline():
    """Mark hosts that haven't checked in for 5 minutes as offline."""
    cutoff = now() - timedelta(minutes=5)
    stale = Host.objects.filter(
        status=Host.Status.ONLINE,
        last_checkin__lt=cutoff,
    )
    count = stale.update(status=Host.Status.OFFLINE)
    if count:
        logger.info("Marked %d stale hosts as offline", count)
    return f"{count} hosts marked offline"
