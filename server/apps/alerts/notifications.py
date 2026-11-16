import logging

from . import channels
from .models import NotificationChannel

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


def message_for(payload: dict, host=None) -> "channels.Message":
    """A channel Message from a webhook-style payload: every channel words the
    same facts, and the webhook still receives the payload unchanged."""
    from django.conf import settings

    event = payload.get("event") or "firing"
    severity = str(payload.get("severity") or "info")
    site = ""
    if host is not None:
        from vigil import scoping

        owner = scoping.scope_of(host)
        if owner is not None and not getattr(owner, "is_global", False):
            site = owner.name
    base = getattr(settings, "VIGIL_PUBLIC_URL", "") or ""
    link = ""
    if base and payload.get("host_id"):
        link = f"{base}/hosts/{payload['host_id']}/"
    verb = {"firing": "Alert", "resolved": "Resolved", "notification": "Notice",
            "test": "Test"}.get(event, event.title())
    return channels.Message(
        event=event, severity=severity,
        title=f"{verb}: {payload.get('host') or 'Vigil'}",
        text=str(payload.get("message") or ""),
        host=str(payload.get("host") or ""), site=site,
        value=payload.get("metric_value"), rule=str(payload.get("rule") or ""),
        link=link, payload=payload)


def deliver(channel, message) -> bool:
    """Send *message* through *channel* and record the outcome on the row.

    Never raises: one broken channel must not cost the others their message.
    A channel whose type is unknown or needs a feature this install does not
    have is skipped and says why.
    """
    from django.utils.timezone import now

    from vigil import licensing

    kind = channels.get(channel.kind)
    ok, status = False, ""
    if kind is None:
        status = f"unknown channel type {channel.kind!r}"
    elif kind.feature and not licensing.has_feature(kind.feature):
        status = f"{kind.label} needs a license that includes it"
    else:
        token = channels.allow_private.set(channel.allow_private_targets)
        try:
            kind.send(channels.plain_config(kind, channel.config), message)
            ok, status = True, "delivered"
        except channels.ChannelError as exc:
            status = str(exc)
        except Exception:  # noqa: BLE001 — a channel bug must not stop the rest
            logger.exception("channel %s (%s) failed", channel.pk, channel.kind)
            status = "failed unexpectedly; see the server log"
        finally:
            channels.allow_private.reset(token)
    if ok:
        logger.info("notification sent via %s (%s)", channel.name, channel.kind)
    else:
        logger.error("notification via %s (%s) failed: %s", channel.name, channel.kind, status)
    # Test sends count too: the admin pressing "Send test" wants the row to
    # show whether it works.
    type(channel).objects.filter(pk=channel.pk).update(
        last_ok=ok, last_status=status[:200], last_status_at=now())
    return ok


def _channels_for_host(rows, host):
    """The channels that may hear about *host* (S1 05b, decisions-06 item
    18): global channels, and channels of the host's own site. A Lab alert
    never reaches a West channel. Without a host only global channels hear."""
    from vigil import scoping

    rows = list(rows)
    if host is None:
        return [c for c in rows if scoping.site_id_of(c) is None]
    scoping.prime_site_ids([host])
    return [c for c in rows if scoping.within_site(c, host)]


def dispatch_alert_notification(alert, event="firing"):
    """Send notification to all enabled channels for an alert event.

    event: "firing" or "resolved"

    Returns False when the host is in maintenance and nothing was sent, True
    otherwise (whether or not any channel was set up to receive it).
    """
    # Imported here to avoid an import cycle: maintenance imports the models,
    # and this module is imported by tasks, which maintenance does not need.
    from .maintenance import in_maintenance

    if alert.host_id and in_maintenance(alert.host):
        return False

    targets = NotificationChannel.objects.filter(enabled=True)
    if event == "firing":
        targets = targets.filter(on_firing=True)
    elif event == "resolved":
        targets = targets.filter(on_resolved=True)

    message = message_for(_build_payload(alert, event), alert.host)

    for channel in _channels_for_host(targets, alert.host):
        deliver(channel, message)

    # Every alert source funnels through here, so this is the one place the
    # event bus can be fed from. It had two subscribers and no emitter:
    # alert-triggered automations never ran and "alert.fired" never reached
    # the audit log. Handlers are isolated by hooks.emit, so a subscriber
    # raising cannot cost a notification that already went out.
    from vigil import hooks
    hooks.emit("alert_sent" if event == "firing" else "alert_resolved",
               alert=alert)
    return True


#: How many hosts are named in a grouped notice before "and N more".
GROUP_NAMES_SHOWN = 10


def dispatch_offline_group(alerts, event="firing") -> list:
    """One notice for several offline alerts of hosts of one site.

    event "firing" says the hosts stopped checking in, "resolved" that they
    are checking in again. Hosts in maintenance are left out, as
    dispatch_alert_notification leaves them out. Returns the alerts the notice
    covered (empty when every host was in maintenance and nothing was sent).
    """
    from .maintenance import in_maintenance

    covered = [a for a in alerts if not (a.host_id and in_maintenance(a.host))]
    if not covered:
        return []
    first = covered[0]
    names = sorted(a.host.hostname for a in covered)
    shown = ", ".join(names[:GROUP_NAMES_SHOWN])
    if len(names) > GROUP_NAMES_SHOWN:
        shown += f" and {len(names) - GROUP_NAMES_SHOWN} more"

    targets = NotificationChannel.objects.filter(enabled=True)
    if event == "firing":
        targets = targets.filter(on_firing=True)
    else:
        targets = targets.filter(on_resolved=True)

    payload = _build_payload(first, event)
    payload.update({
        "alert_id": None,
        "alert_ids": [str(a.id) for a in covered],
        "host": f"{len(covered)} hosts",
        "host_id": None,
        "hosts": names,
        "metric_value": None,
    })
    base = message_for(payload, first.host)
    where = f" in {base.site}" if base.site else ""
    if event == "firing":
        text = f"{len(covered)} hosts{where} stopped checking in: {shown}"
    else:
        text = f"{len(covered)} hosts{where} are checking in again: {shown}"
    payload["message"] = text

    # Not dataclasses.replace(base, ...): it copies the title too, which says
    # one host's name, and shares this message's payload by reference.
    message = channels.Message(
        event=event,
        title=f"Alert: {payload['host']}",
        text=text,
        severity=base.severity,
        host=base.host,
        payload=payload,
        site=base.site,
    )
    for channel in _channels_for_host(targets, first.host):
        deliver(channel, message)

    # Each alert still feeds the event bus, as one-by-one dispatch does, so
    # alert-triggered automations and the audit log see every host.
    from vigil import hooks
    for alert in covered:
        hooks.emit("alert_sent" if event == "firing" else "alert_resolved",
                   alert=alert)
    return covered
