"""Thin HTTP client for Jackil's ticket API.

Jackil authenticates with ``Authorization: Api-Key <key>`` and exposes
``/api/v1/tickets/`` plus ``/api/v1/tickets/<id>/messages/``. The key's user
must be an agent or admin — Jackil's ``IsStaff`` refuses anyone else, and it
answers 403, which is worth saying out loud in the error rather than making
someone guess at a bare status code.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import requests

from apps.instance.config import setting

logger = logging.getLogger(__name__)

TIMEOUT = 15

#: A drawer must not sit waiting on a helpdesk that is down. Writes keep the
#: long timeout; this read is decorative and four seconds is its whole budget.
RELATED_TIMEOUT = 4


class JackilError(RuntimeError):
    """A Jackil call failed. The message is written to be shown to a human."""


class JackilUnreachable(JackilError):
    """No answer at all: refused, timed out, or the name did not resolve."""


class JackilNotFound(JackilError):
    """Jackil answered 404: a wrong URL, or a ticket that no longer exists."""


def configured() -> bool:
    return bool(setting("JACKIL_URL") and setting("JACKIL_API_KEY"))


def enabled() -> bool:
    return bool(setting("JACKIL_ENABLED")) and configured()


def _base() -> str:
    return (setting("JACKIL_URL") or "").rstrip("/")


def _headers() -> dict:
    return {
        "Authorization": f"Api-Key {setting('JACKIL_API_KEY')}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, payload: dict | None = None,
             timeout: int = TIMEOUT):
    url = f"{_base()}/api/v1{path}"
    try:
        resp = requests.request(
            method, url, json=payload, headers=_headers(),
            verify=setting("JACKIL_VERIFY_SSL"), timeout=timeout,
        )
    except requests.RequestException as exc:
        raise JackilUnreachable(f"Could not reach {_base()} — {exc}") from exc

    if resp.status_code == 401:
        raise JackilError("Jackil rejected the API key.")
    if resp.status_code == 403:
        raise JackilError(
            "The API key works but its user is not an agent or admin in Jackil.")
    if resp.status_code == 404:
        raise JackilNotFound(
            f"{url} was not found — check the URL, and that Jackil has its API "
            f"feature enabled.")
    if resp.status_code >= 400:
        raise JackilError(f"Jackil answered {resp.status_code}: {resp.text[:200]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise JackilError("Jackil did not answer with JSON — is that URL Jackil?") from exc


def ping() -> dict:
    """Cheapest call that proves URL, key and role in one go.

    A single-row listing, not a create: a connection test must never leave a
    ticket behind in someone's queue.
    """
    if not configured():
        raise JackilError("Set the Jackil URL and an API key first.")
    return _request("GET", "/tickets/?page_size=1")


def create_ticket(*, title: str, description: str, priority: str,
                  requester_email: str = "", tags: str = "") -> dict:
    body = {
        "title": title[:255],
        "description": description,
        "priority": priority,
        "status": "open",
    }
    if requester_email:
        body["requester_email"] = requester_email
    if tags:
        body["tags"] = tags
    return _request("POST", "/tickets/", body)


def add_note(ticket_id: int, body: str) -> dict:
    """Post an internal note — not a public reply.

    "The alert cleared" is for whoever is working the ticket, not something to
    email the requester about.
    """
    return _request("POST", f"/tickets/{ticket_id}/messages/",
                    {"body": body, "kind": "note"})


def get_ticket(ticket_id: int) -> dict:
    return _request("GET", f"/tickets/{ticket_id}/")


def set_status(ticket_id: int, status: str) -> dict:
    return _request("PATCH", f"/tickets/{ticket_id}/", {"status": status})


def related(kind: str, obj_id: str) -> dict:
    """What Jackil has linked to one of Vigil's objects.

    A 404 here means the far end predates ``/api/v1/vigil/links/`` — Jackil
    2026.3 or later is what offers it — which is a sentence worth more than
    "Not found": the remedy is to update Jackil, not to re-check keys.
    """
    try:
        return _request(
            "GET", "/vigil/links/?" + urlencode({"type": kind, "id": obj_id}),
            timeout=RELATED_TIMEOUT,
        )
    except JackilNotFound as exc:
        raise JackilError(
            "Jackil does not offer related links yet. "
            "Update Jackil to 2026.3 or later."
        ) from exc
    except JackilUnreachable as exc:
        # Shown to anyone who opens a host, not just the admin who set the
        # link up, so it says what happened rather than quoting urllib3.
        raise JackilError("Jackil is not answering right now.") from exc


def ticket_url(ticket_id: int) -> str:
    return f"{_base()}/tickets/{ticket_id}/" if _base() else ""
