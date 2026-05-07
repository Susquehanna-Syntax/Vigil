import logging
import operator as op
from datetime import timedelta

from celery import shared_task
from django.conf import settings
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
    stale_list = list(stale)
    count = stale.update(status=Host.Status.OFFLINE)
    if count:
        logger.info("Marked %d stale hosts as offline", count)
        for host in stale_list:
            already_firing = Alert.objects.filter(
                host=host,
                rule=None,
                state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
                message__startswith="Host offline:",
            ).exists()
            if not already_firing:
                Alert.objects.create(
                    host=host,
                    rule=None,
                    state=Alert.State.FIRING,
                    severity="warning",
                    message=f"Host offline: {host.hostname} has not checked in for 5+ minutes",
                    metric_value=None,
                )
    return f"{count} hosts marked offline"


@shared_task(name="metrics.prune_old_metric_points")
def prune_old_metric_points():
    """Delete MetricPoints older than VIGIL_METRIC_RETENTION_DAYS (default: 30)."""
    retention_days = getattr(settings, "VIGIL_METRIC_RETENTION_DAYS", 30)
    cutoff = now() - timedelta(days=retention_days)
    deleted, _ = MetricPoint.objects.filter(time__lt=cutoff).delete()
    if deleted:
        logger.info("Pruned %d metric points older than %d days", deleted, retention_days)
    return f"Pruned {deleted} metric points older than {retention_days} days"


# ---------------------------------------------------------------------------
# Docker image update alerts
# ---------------------------------------------------------------------------

def _get_or_create_docker_rule() -> AlertRule:
    """Return the sentinel AlertRule for Docker outdated-image alerts.

    enabled=False so the standard metric evaluation engine ignores it;
    check_docker_image_updates() manages firing and resolving directly.
    """
    rule, _ = AlertRule.objects.get_or_create(
        name="Docker: Outdated Image",
        defaults={
            "category": "docker",
            "metric": "image_outdated",
            "operator": "gt",
            "threshold": 0,
            "severity": AlertRule.Severity.WARNING,
            "duration_seconds": 0,
            "enabled": False,
            "is_default": True,
        },
    )
    return rule


@shared_task(name="alerts.check_docker_image_updates")
def check_docker_image_updates():
    """Evaluate docker/image_outdated metrics and fire or resolve per-container alerts."""
    rule = _get_or_create_docker_rule()
    online_hosts = Host.objects.filter(status=Host.Status.ONLINE)
    window = now() - timedelta(minutes=15)
    fired = resolved = 0

    for host in online_hosts:
        points = MetricPoint.objects.filter(
            host=host,
            category="docker",
            metric="image_outdated",
            time__gte=window,
        ).order_by("-time")

        # Latest point per container (ordered desc, first-seen wins)
        latest_by_container: dict[str, MetricPoint] = {}
        for pt in points:
            key = pt.labels.get("container_name", "")
            if key and key not in latest_by_container:
                latest_by_container[key] = pt

        for container_name, pt in latest_by_container.items():
            image = pt.labels.get("image", "unknown")
            existing = Alert.objects.filter(
                host=host,
                rule=rule,
                state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
                message__contains=f"'{container_name}'",
            ).first()

            if pt.value >= 1.0 and not existing:
                alert = Alert.objects.create(
                    host=host,
                    rule=rule,
                    state=Alert.State.FIRING,
                    severity=AlertRule.Severity.WARNING,
                    message=f"Docker: Container '{container_name}' is running an outdated image ({image})",
                    metric_value=1.0,
                    fix_context={"container_name": container_name, "image": image},
                )
                fired += 1
                logger.info("Docker alert fired: %s on %s (%s)", container_name, host.hostname, image)
                dispatch_alert_notification(alert, event="firing")

            elif pt.value == 0.0 and existing and existing.state == Alert.State.FIRING:
                existing.state = Alert.State.RESOLVED
                existing.resolved_at = now()
                existing.save(update_fields=["state", "resolved_at"])
                resolved += 1
                logger.info("Docker alert resolved: %s on %s", container_name, host.hostname)
                dispatch_alert_notification(existing, event="resolved")

    return f"Docker image check: {fired} fired, {resolved} resolved"


# ---------------------------------------------------------------------------
# Outdated agent version alerts
# ---------------------------------------------------------------------------

@shared_task(name="alerts.check_outdated_agents")
def check_outdated_agents():
    """Fire an alert for any online host running an older agent version."""
    from django.conf import settings as _s
    current_version = getattr(_s, "VIGIL_AGENT_VERSION", "")
    if not current_version:
        return "VIGIL_AGENT_VERSION not set, skipping"

    online_hosts = Host.objects.filter(status=Host.Status.ONLINE)
    fired = resolved = 0

    for host in online_hosts:
        if not host.agent_version:
            continue

        is_outdated = host.agent_version != current_version

        existing = Alert.objects.filter(
            host=host,
            rule=None,
            state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
            message__startswith="Agent outdated:",
        ).first()

        if is_outdated and not existing:
            Alert.objects.create(
                host=host,
                rule=None,
                state=Alert.State.FIRING,
                severity="warning",
                message=f"Agent outdated: {host.hostname} is running v{host.agent_version} (current: v{current_version})",
                metric_value=None,
                fix_context={"host_id": str(host.id), "hostname": host.hostname},
            )
            fired += 1
            logger.info("Agent outdated alert fired: %s (v%s → v%s)", host.hostname, host.agent_version, current_version)

        elif not is_outdated and existing and existing.state == Alert.State.FIRING:
            existing.state = Alert.State.RESOLVED
            existing.resolved_at = now()
            existing.save(update_fields=["state", "resolved_at"])
            resolved += 1
            logger.info("Agent outdated alert resolved: %s", host.hostname)

    return f"Agent version check: {fired} fired, {resolved} resolved"
