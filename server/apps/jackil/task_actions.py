"""jackil_ticket / jackil_note / jackil_resolve — marker task actions (2026.14.6).

The trust split is QA-38's: ``key``, ``priority`` and ``status`` come from the
task the server signed, never from what the agent reports. ``title``,
``description``, ``body`` and ``note`` may carry ``${{ steps… }}`` references
only the agent can resolve, so their text comes from the step's reported
result through ``reported_text``, capped and stored as plain text.

The link row (``JackilWorkTicket``) is the dedupe: a re-fire finds this host's
open ticket for the key and notes it instead of opening another. Like the
alert→ticket integration in ``service.py``, nothing here may break task
result handling — a Jackil outage is a logged line, never a raised error.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError
from django.utils.timezone import now

from apps.alerts.task_actions import fired_steps, reported_text
from apps.instance.config import setting

from . import client
from .models import JackilWorkTicket

logger = logging.getLogger("vigil.jackil")

ACTIONS = ("jackil_ticket", "jackil_note", "jackil_resolve")

#: ``Jackil/apps/tickets/models.py``'s own vocabulary.
PRIORITIES = ("critical", "high", "medium", "low")
RESOLVE_STATUSES = ("resolved", "closed")

#: ``key`` is capped to ``JackilWorkTicket.key``'s width; the text fields match
#: what the agent caps them at, so the reported text and the stored one agree.
KEY_CAP = 80
TITLE_CAP = 200
TEXT_CAP = 4000


def apply(task) -> None:
    """Carry out every fired Jackil step of *task*.

    Each step runs in its own guard: one malformed step must not cost the
    operator the tickets the other steps asked for.
    """
    steps = fired_steps(task, ACTIONS)
    if not steps:
        return
    if not client.enabled():
        logger.warning("Jackil is not enabled — skipping %d ticket step(s) on task %s",
                       len(steps), task.pk)
        return
    for signed, result in steps:
        action = signed.get("action")
        try:
            if action == "jackil_ticket":
                _ticket(task, signed, result)
            elif action == "jackil_note":
                _note(task, signed, result)
            elif action == "jackil_resolve":
                _resolve(task, signed, result)
        except client.JackilError as exc:
            logger.error("Jackil: %s on task %s failed: %s", action, task.pk, exc)
        except Exception:
            logger.exception("Jackil task action %s failed on task %s", action, task.pk)


def _key(signed: dict) -> str:
    return str((signed.get("params") or {}).get("key", "")).strip()[:KEY_CAP]


def _open_link(task, key: str) -> JackilWorkTicket | None:
    return JackilWorkTicket.objects.filter(
        host=task.host, key=key, closed_at__isnull=True).first()


def _ticket(task, signed: dict, result: dict) -> None:
    """Open this host's ticket for the key, or note the repeat on its open one."""
    signed_params = signed.get("params") or {}
    key = _key(signed)
    if not key:
        logger.warning("jackil_ticket on task %s: no key — step skipped", task.pk)
        return
    title = reported_text(signed_params, result, "title", TITLE_CAP)
    if not title:
        logger.warning("jackil_ticket on task %s: empty title — step skipped", task.pk)
        return
    priority = str(signed_params.get("priority", "")).strip().lower()
    if priority not in PRIORITIES:
        priority = "medium"
    description = reported_text(signed_params, result, "description", TEXT_CAP)

    open_ = _open_link(task, key)
    if open_ is not None:
        note = f"Vigil: this happened again on {task.host.hostname}: {title}"
        if description:
            note = f"{note}\n{description}"
        client.add_note(open_.ticket_id, note)
        return

    ticket = client.create_ticket(
        title=f"{task.host.hostname}: {title}"[:TITLE_CAP],
        description="\n".join(filter(None, [
            description,
            "",
            f"Host: {task.host.hostname}",
            f"Key: {key}",
            f"Opened by Vigil task {task.id}.",
        ])),
        priority=priority,
        requester_email=setting("JACKIL_REQUESTER_EMAIL") or "",
        tags=setting("JACKIL_TAGS") or "",
    )
    ticket_id = ticket.get("id")
    if not ticket_id:
        logger.error("Jackil: ticket created for task %s but no id came back", task.pk)
        return
    try:
        JackilWorkTicket.objects.create(
            host=task.host, key=key, ticket_id=ticket_id,
            url=client.ticket_url(ticket_id))
    except IntegrityError:
        # Another worker won the race and already linked an open ticket for
        # this host and key. The ticket we just opened is the duplicate, so
        # say so on it rather than leaving an orphan nobody can explain.
        logger.warning("Jackil: duplicate ticket %s for host %s key %s",
                       ticket_id, task.host_id, key)
        try:
            client.add_note(ticket_id, "Duplicate — Vigil had already opened a "
                                       "ticket for this host and key.")
            client.set_status(ticket_id, "closed")
        except client.JackilError:
            pass


def _note(task, signed: dict, result: dict) -> None:
    """Add an internal note to this host's open ticket for the key."""
    signed_params = signed.get("params") or {}
    key = _key(signed)
    body = reported_text(signed_params, result, "body", TEXT_CAP)
    if not key or not body:
        logger.warning("jackil_note on task %s: no %s — step skipped",
                       task.pk, "key" if not key else "body")
        return
    link = _open_link(task, key)
    if link is None:
        logger.info("jackil_note on task %s: no open ticket for key %r — step skipped",
                    task.pk, key)
        return
    client.add_note(link.ticket_id, body)


def _resolve(task, signed: dict, result: dict) -> None:
    """Note the resolution, set the status, then close the link.

    ``closed_at`` is written only after Jackil accepts the status change: if
    the call raises, the link stays open so the next run tries again.
    """
    signed_params = signed.get("params") or {}
    key = _key(signed)
    if not key:
        logger.warning("jackil_resolve on task %s: no key — step skipped", task.pk)
        return
    link = _open_link(task, key)
    if link is None:
        return
    note = reported_text(signed_params, result, "note", TEXT_CAP)
    if note:
        client.add_note(link.ticket_id, note)
    status = str(signed_params.get("status", "")).strip().lower()
    if status not in RESOLVE_STATUSES:
        status = "resolved"
    client.set_status(link.ticket_id, status)
    link.closed_at = now()
    link.save(update_fields=["closed_at"])


__all__ = ["ACTIONS", "apply"]
