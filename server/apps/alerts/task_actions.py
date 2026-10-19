"""raise_alert / resolve_alert / send_notification — marker task actions the server carries out (2026.14.6).

``severity``, ``key`` and ``channel`` come from the signed task the server stored
(the ``params`` of the matching step in ``task.params``), never from what the
agent reports. The message is different: it may contain
``${{ steps.<id>.result.<field> }}``, which only the agent can resolve, so the
server takes the agent-reported ``result.message`` for that step. It caps it at
:data:`MESSAGE_CAP` characters and stores it as plain text.

If the agent reported no message the server uses the signed text. An agent can
therefore change the *wording* of an alert it raises, but never its severity, its
key or where it goes.

A step fires only when the agent reported its ``status`` as ``"ok"``, on any
terminal task state — a step that ran before a later step failed still fires, and
a step skipped by ``when:`` does not.
"""

from __future__ import annotations

import logging

from django.utils.timezone import now

from .models import Alert, AlertRule, NotificationChannel
from .notifications import _DISPATCHERS, dispatch_alert_notification

logger = logging.getLogger(__name__)

#: A reported message longer than this is cut, so a hostile agent cannot use an
#: alert's message field as a storage dump.
MESSAGE_CAP = 2000

#: How long a task-authored key is. Matches ``Alert.source_key``.
KEY_CAP = 80

ACTIONS = ("raise_alert", "resolve_alert", "send_notification")

_SEVERITIES = {AlertRule.Severity.INFO, AlertRule.Severity.WARNING,
               AlertRule.Severity.CRITICAL}

_OPEN_STATES = (Alert.State.FIRING, Alert.State.ACKNOWLEDGED)


def apply(task) -> None:
    """Carry out every fired alert/notification step of *task*.

    Each step runs in its own guard: one malformed step must not cost the
    operator the alerts the other steps asked for.
    """
    for signed, result in fired_steps(task, ACTIONS):
        action = signed.get("action")
        try:
            if action == "raise_alert":
                _raise(task, signed, result)
            elif action == "resolve_alert":
                _resolve(task, signed, result)
            elif action == "send_notification":
                _notify(task, signed, result)
        except Exception:
            logger.exception("alert task action %s failed on task %s", action, task.pk)


def fired_steps(task, actions: tuple[str, ...]) -> list[tuple[dict, dict]]:
    """``(signed_step, reported_result)`` for every fired step of *task*.

    "Fired" means the step is in the signed task *and* the agent reported it with
    ``status == "ok"``. The signed step is what params are read from; the report
    only ever supplies rendered text. A signed step with no report carrying its
    id never fires, which is what keeps a step the agent never ran silent.

    Only ``_script`` tasks are considered: for a legacy single-action task the
    step lookup returns ``task.params`` itself, which has no ``"action"`` key and
    is skipped so no step can fire without an id to match a report against.
    """
    from apps.tasks.views import _tag_steps

    reported = {step.get("id"): step
                for step in (task.result_data or {}).get("steps") or []
                if isinstance(step, dict)}
    fired = []
    for signed in _tag_steps(task, actions):
        if not isinstance(signed, dict) or "action" not in signed:
            continue
        report = reported.get(signed.get("id"))
        if not isinstance(report, dict) or report.get("status") != "ok":
            continue
        result = report.get("result")
        fired.append((signed, result if isinstance(result, dict) else {}))
    return fired


def reported_text(signed_params: dict, result: dict, field: str, cap: int) -> str:
    """The text for *field*: what the agent reported, or the signed text.

    The agent renders ``${{ steps… }}`` markers we cannot resolve here, so its
    report wins when it carries a non-empty string; anything else falls back to
    the signed param. Always stripped and cut to *cap*.
    """
    value = result.get(field)
    if not isinstance(value, str) or not value.strip():
        value = (signed_params or {}).get(field, "")
    return str(value).strip()[:cap]


def _raise(task, signed: dict, result: dict) -> None:
    """Open a rule-less alert on this host and notify, unless one is open."""
    signed_params = signed.get("params") or {}
    severity = str(signed_params.get("severity", "")).strip().lower()
    if severity not in _SEVERITIES:
        logger.warning("raise_alert on task %s: unknown severity %r — step skipped",
                       task.pk, severity)
        return
    key = str(signed_params.get("key", "")).strip()[:KEY_CAP]
    message = reported_text(signed_params, result, "message", MESSAGE_CAP)
    if not message:
        logger.warning("raise_alert on task %s: empty message — step skipped", task.pk)
        return

    open_alerts = Alert.objects.filter(host=task.host, rule__isnull=True,
                                       state__in=_OPEN_STATES)
    if key:
        already = open_alerts.filter(source_key=key)
    else:
        already = open_alerts.filter(source_key="", message=message)
    if already.exists():
        return

    alert = Alert.objects.create(host=task.host, rule=None, severity=severity,
                                 message=message, source_key=key)
    dispatch_alert_notification(alert, "firing")


def _resolve(task, signed: dict, _result: dict) -> None:
    """Resolve this host's open rule-less alert carrying the signed key."""
    key = str((signed.get("params") or {}).get("key", "")).strip()[:KEY_CAP]
    if not key:
        logger.warning("resolve_alert on task %s: no key — step skipped", task.pk)
        return
    for alert in Alert.objects.filter(host=task.host, rule__isnull=True,
                                      source_key=key, state__in=_OPEN_STATES):
        alert.state = Alert.State.RESOLVED
        alert.resolved_at = now()
        alert.save(update_fields=["state", "resolved_at"])
        dispatch_alert_notification(alert, "resolved")


def _notify(task, signed: dict, result: dict) -> None:
    """Send the message to every enabled channel, or only the named one."""
    signed_params = signed.get("params") or {}
    message = reported_text(signed_params, result, "message", MESSAGE_CAP)
    if not message:
        logger.warning("send_notification on task %s: empty message — step skipped",
                       task.pk)
        return
    channel_name = str(signed_params.get("channel", "")).strip()

    channels = NotificationChannel.objects.filter(enabled=True)
    if channel_name:
        channels = channels.filter(name=channel_name)
        if not channels.exists():
            logger.warning("send_notification on task %s: no enabled channel named %r",
                           task.pk, channel_name)
            return

    # A notification has no alert behind it, so the payload's alert fields are
    # empty. Every key _build_payload makes must be present: the senders index
    # them directly.
    payload = {
        "event": "notification",
        "alert_id": None,
        "host": task.host.hostname,
        "host_id": str(task.host_id),
        "severity": AlertRule.Severity.INFO,
        "message": message,
        "metric_value": None,
        "rule": None,
        "fired_at": now().isoformat(),
        "resolved_at": None,
    }
    for channel in channels:
        dispatcher = _DISPATCHERS.get(channel.kind)
        if dispatcher:
            dispatcher(channel, payload)


__all__ = ["ACTIONS", "MESSAGE_CAP", "apply", "fired_steps", "reported_text"]
