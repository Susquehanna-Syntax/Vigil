import json
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

from .models import Alert, NotificationChannel

logger = logging.getLogger(__name__)


def _build_payload(alert, event):
    """Build a notification payload dict for an alert event."""
    return {
        "event": event,
        "alert_id": str(alert.id),
        "host": alert.host.hostname,
        "host_id": str(alert.host_id),
        "severity": alert.severity,
        "message": alert.message,
        "metric_value": alert.metric_value,
        "rule": alert.rule.name if alert.rule else None,
        "fired_at": alert.fired_at.isoformat() if alert.fired_at else None,
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
    }


def _send_webhook(channel, payload):
    url = channel.config.get("url")
    if not url:
        logger.warning("Webhook channel %s has no URL configured", channel.name)
        return

    headers = {"Content-Type": "application/json"}
    if secret := channel.config.get("secret"):
        headers["X-Vigil-Secret"] = secret

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook sent to %s (status %d)", channel.name, resp.status_code)
    except requests.RequestException as e:
        logger.error("Webhook failed for %s: %s", channel.name, e)


def _send_email(channel, payload):
    recipients = channel.config.get("recipients", [])
    if not recipients:
        logger.warning("Email channel %s has no recipients configured", channel.name)
        return

    severity = payload["severity"].upper()
    event = payload["event"].upper()
    subject = f"[Vigil {event}] [{severity}] {payload['host']}: {payload['message']}"
    body = (
        f"Host: {payload['host']}\n"
        f"Severity: {severity}\n"
        f"Event: {event}\n"
        f"Message: {payload['message']}\n"
        f"Metric Value: {payload['metric_value']}\n"
        f"Rule: {payload['rule']}\n"
        f"Fired At: {payload['fired_at']}\n"
    )
    if payload.get("resolved_at"):
        body += f"Resolved At: {payload['resolved_at']}\n"

    try:
        # The connection is built explicitly rather than left to Django's
        # settings lookup, so SMTP entered in the UI is actually used.
        from apps.instance.config import mail_connection, setting
        send_mail(
            subject=subject,
            message=body,
            from_email=setting("VIGIL_NOTIFICATION_FROM_EMAIL"),
            recipient_list=recipients,
            connection=mail_connection(),
        )
        logger.info("Email sent via channel %s to %d recipients", channel.name, len(recipients))
    except Exception as e:
        logger.error("Email failed for %s: %s", channel.name, e)


_DISPATCHERS = {
    NotificationChannel.Kind.WEBHOOK: _send_webhook,
    NotificationChannel.Kind.EMAIL: _send_email,
}


def dispatch_alert_notification(alert, event="firing"):
    """Send notification to all enabled channels for an alert event.

    event: "firing" or "resolved"
    """
    channels = NotificationChannel.objects.filter(enabled=True)
    if event == "firing":
        channels = channels.filter(on_firing=True)
    elif event == "resolved":
        channels = channels.filter(on_resolved=True)

    payload = _build_payload(alert, event)

    for channel in channels:
        dispatcher = _DISPATCHERS.get(channel.kind)
        if dispatcher:
            dispatcher(channel, payload)

    # Every alert source funnels through here, so this is the one place the
    # event bus can be fed from. It had two subscribers and no emitter:
    # alert-triggered automations never ran and "alert.fired" never reached
    # the audit log. Handlers are isolated by hooks.emit, so a subscriber
    # raising cannot cost a notification that already went out.
    from vigil import hooks
    hooks.emit("alert_sent" if event == "firing" else "alert_resolved",
               alert=alert)
