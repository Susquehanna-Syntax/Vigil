"""Turn alert events into Jackil tickets.

Two rules shape everything here:

1. **Never break alerting.** Notification dispatch already runs inside a Celery
   task, so a network call is not on anyone's request path, but a helpdesk
   being down must still not cost an alert. Every entry point swallows its
   own failures into the log.
2. **One alert, one ticket.** The link row is the dedupe; a re-fire finds the
   existing ticket rather than opening another.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import IntegrityError
from django.utils.timezone import now

from apps.instance.config import setting

from . import client
from .models import JackilTicket

logger = logging.getLogger("vigil.jackil")

#: Vigil severity → Jackil priority. Jackil has a "medium" that nothing here
#: maps to: Vigil's three levels are already the operator's judgement, and
#: inventing a fourth would only blur it.
PRIORITY = {"critical": "critical", "warning": "high", "info": "low"}

#: Ranked so "open a ticket from warning up" is a comparison, not a table.
_RANK = {"info": 0, "warning": 1, "critical": 2}


def severity_allowed(severity: str) -> bool:
    floor = _RANK.get(str(setting("JACKIL_MIN_SEVERITY") or "critical"), 2)
    return _RANK.get(severity, 0) >= floor


def _describe(alert) -> str:
    lines = [
        alert.message,
        "",
        f"Host: {alert.host.hostname}",
        f"Severity: {alert.severity}",
    ]
    if alert.rule_id and alert.rule:
        lines.append(f"Rule: {alert.rule.name}")
    if alert.metric_value is not None:
        lines.append(f"Measured: {alert.metric_value}")
    if alert.fired_at:
        lines.append(f"Fired at: {alert.fired_at.isoformat()}")
    lines += ["", f"Opened by Vigil. Alert id {alert.id}."]
    return "\n".join(lines)


def on_alert_sent(alert) -> JackilTicket | None:
    """Open a ticket for *alert*, unless one already exists."""
    if not client.enabled() or not severity_allowed(alert.severity):
        return None
    existing = JackilTicket.objects.filter(alert=alert).first()
    if existing:
        return existing

    try:
        ticket = client.create_ticket(
            title=f"[{alert.severity.upper()}] {alert.host.hostname}: {alert.message}",
            description=_describe(alert),
            priority=PRIORITY.get(alert.severity, "medium"),
            requester_email=setting("JACKIL_REQUESTER_EMAIL") or "",
            tags=setting("JACKIL_TAGS") or "",
        )
    except client.JackilError as exc:
        logger.error("Jackil: could not open a ticket for alert %s: %s", alert.id, exc)
        return None

    ticket_id = ticket.get("id")
    if not ticket_id:
        logger.error("Jackil: ticket created for alert %s but no id came back", alert.id)
        return None

    try:
        return JackilTicket.objects.create(
            alert=alert, ticket_id=ticket_id, url=client.ticket_url(ticket_id))
    except IntegrityError:
        # Another worker won the race and already linked this alert. The
        # ticket we just opened is the duplicate, so say so on it rather than
        # leaving an orphan nobody can explain.
        logger.warning("Jackil: duplicate ticket %s for alert %s", ticket_id, alert.id)
        try:
            client.add_note(ticket_id, "Duplicate — Vigil had already opened a "
                                       "ticket for this alert.")
            client.set_status(ticket_id, "closed")
        except client.JackilError:
            pass
        return JackilTicket.objects.filter(alert=alert).first()


def on_alert_resolved(alert) -> None:
    """Note the clear on the ticket, and optionally resolve it."""
    if not client.enabled():
        return
    link = JackilTicket.objects.filter(alert=alert).first()
    if link is None or link.cleared_at:
        # No ticket, or the clear was already reported. A flapping alert must
        # not post the same note a dozen times.
        return

    resolved_at = alert.resolved_at.isoformat() if alert.resolved_at else now().isoformat()
    # Inside a flap (a re-fire was noted this hour) the clear changes the
    # ticket's state but adds no note: one "fired again" line already says
    # the alert is bouncing.
    flapping = bool(cache.get(f"jackil:refire-noted:{link.pk}"))
    try:
        if not flapping:
            client.add_note(link.ticket_id, f"Vigil: this alert cleared at {resolved_at}.")
        if setting("JACKIL_RESOLVE_ON_CLEAR"):
            client.set_status(link.ticket_id, "resolved")
    except client.JackilError as exc:
        logger.error("Jackil: could not update ticket %s: %s", link.ticket_id, exc)
        return

    link.cleared_at = now()
    link.save(update_fields=["cleared_at"])


#: A flapping alert can re-fire every few minutes; the ticket hears about it
#: at most once in this window rather than once per flap.
REFIRE_NOTE_SECONDS = 3600


def on_alert_refired(alert) -> None:
    """Note on the ticket that a cleared alert came back, and reopen it.

    Throttled per ticket: the flap window exists so a noisy metric does not
    page anyone, and the helpdesk deserves the same courtesy.
    """
    if not client.enabled():
        return
    link = JackilTicket.objects.filter(alert=alert).first()
    if link is None:
        return
    throttle_key = f"jackil:refire-noted:{link.pk}"
    noting = not cache.get(throttle_key)
    reopening = bool(link.cleared_at) and bool(setting("JACKIL_RESOLVE_ON_CLEAR"))

    flaps = alert.flap_count or 0
    times = "1 time" if flaps == 1 else f"{flaps} times"
    try:
        # The throttle only quietens the note. The ticket's state follows the
        # alert every time, or a flap inside the hour would leave it resolved
        # while the alert fires and swallow the next clear.
        if noting:
            client.add_note(
                link.ticket_id,
                f"Vigil: this alert fired again at {now().isoformat()} "
                f"({times} in the flap window).")
        if reopening:
            client.set_status(link.ticket_id, "open")
    except client.JackilError as exc:
        logger.error("Jackil: could not note the re-fire on ticket %s: %s",
                     link.ticket_id, exc)
        return

    if noting:
        cache.set(throttle_key, True, REFIRE_NOTE_SECONDS)
    if link.cleared_at:
        # Back to firing, so the next clear is news again.
        link.cleared_at = None
        link.save(update_fields=["cleared_at"])

