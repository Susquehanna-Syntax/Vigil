"""Alerts raised for agent-less device checks (S9).

A ``DeviceCheck`` whose two newest results both failed opens a ``DeviceAlert``
on the device's behalf; the alert resolves when a passing result arrives. It is
rule-less and device-bound, so it follows the outdated-agent pattern in tasks.py
(open/resolve directly) rather than the metric-rule engine.
"""

import logging

from django.db import transaction
from django.utils.timezone import now

from apps.hosts.models import DeviceCheck
from vigil import scoping

from .models import DeviceAlert
from .notifications import dispatch_device_alert

logger = logging.getLogger(__name__)

#: The message carries a poller-supplied detail; keep it inside a sane row.
MAX_MESSAGE = 500

#: How the UI labels a check, reused verbatim in the alert message.
_KIND_WORDS = {"icmp": "Ping", "tcp": "TCP", "http": "HTTP", "dns": "DNS"}


def _target(check) -> str:
    params = check.params if isinstance(check.params, dict) else {}
    if check.kind == "tcp":
        return f"{params.get('host', '')}:{params.get('port', '')}"
    for key in ("host", "url", "name"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _open_alert(check):
    return DeviceAlert.objects.filter(
        device_check=check,
        state__in=[DeviceAlert.State.FIRING, DeviceAlert.State.ACKNOWLEDGED],
    ).first()


def _message(check, latest) -> str:
    """``"<device> is down: <kind word> <target> failed twice: <detail>"`.

    The parts are built first and only the detail is cut to what is left, so
    the sentence never loses the words that make it readable — a poller's
    detail can be 300 characters.
    """
    device = check.device
    head = (f"{device.name} is down: "
            f"{_KIND_WORDS.get(check.kind, check.kind)} {_target(check)} failed twice: ")
    room = MAX_MESSAGE - len(head)
    detail = latest.detail if room > 0 else ""
    return head + detail[:room]


def evaluate(check_ids) -> tuple[int, int]:
    """Open or resolve device alerts after a batch of check results arrived.

    Returns (fired, resolved). Only enabled checks are looked at: a disabled
    check says nothing about the device.

    Runs in the Celery worker (``queue_evaluation``), under a system scope:
    the worker has no request user, and a poller's results can concern any
    device of its site.
    """
    fired = resolved = 0
    with scoping.system(reason="device alert evaluation: every site's devices are polled by their own site's pollers"):
        checks = DeviceCheck.objects.filter(
            enabled=True, id__in=set(check_ids)).select_related("device")
        for check in checks:
            results = list(check.results.order_by("-time", "-id")[:2])
            if len(results) < 2:
                continue
            latest = results[0]
            open_alert = _open_alert(check)
            if not latest.ok and not results[1].ok and open_alert is None:
                alert = DeviceAlert.objects.create(
                    device=check.device, device_check=check,
                    state=DeviceAlert.State.FIRING, message=_message(check, latest),
                )
                fired += 1
                logger.info("Device alert fired: %s", alert.message)
                dispatch_device_alert(alert, "firing")
            elif latest.ok and open_alert is not None:
                open_alert.state = DeviceAlert.State.RESOLVED
                open_alert.resolved_at = now()
                open_alert.save(update_fields=["state", "resolved_at"])
                resolved += 1
                logger.info("Device alert resolved for check %s", check.id)
                dispatch_device_alert(open_alert, "resolved")
    return fired, resolved


def resolve_for_check(check) -> int:
    """Quietly close a check's open alerts — no notifications.

    Called when a check is disabled or deleted: nobody is polling it any more,
    so an alert that keeps saying "down" would be a claim about a device nobody
    is watching rather than news. On delete this runs *before* the row goes
    away, because ``DeviceAlert.device_check`` is SET_NULL and the alerts would
    otherwise lose the only handle evaluation has on them.
    """
    count = 0
    for alert in DeviceAlert.objects.filter(
            device_check=check,
            state__in=[DeviceAlert.State.FIRING, DeviceAlert.State.ACKNOWLEDGED]):
        alert.state = DeviceAlert.State.RESOLVED
        alert.resolved_at = now()
        alert.save(update_fields=["state", "resolved_at"])
        count += 1
    return count


def queue_evaluation(check_ids) -> None:
    """Evaluate these checks in the worker once the results are committed.

    Not inline: a webhook send can take up to 10 seconds, and the poller's
    check-in must never wait on a notification. A broker that cannot take the
    task is logged and costs the check-in nothing; the next results for the
    check evaluate it again.
    """
    ids = sorted(str(i) for i in check_ids)

    def send():
        try:
            from .tasks import evaluate_device_alerts
            evaluate_device_alerts.delay(ids)
        except Exception:
            logger.exception("Could not queue device alert evaluation for %d checks", len(ids))

    transaction.on_commit(send)
