"""jackil_ticket / jackil_note / jackil_resolve — marker actions (2026.14.6).

None of these touch this machine: a helpdesk ticket is server-side state. The
agent reports the step and the server works the ticket when the result
arrives, taking key, priority and status from the task it signed rather than
from anything reported here.

Params arrive already rendered by the runtime (``runtime.resolve_params``), so
``params["title"]`` is the text an operator will read. The caps here match the
server's, so the reported text and the stored one agree.
"""

from __future__ import annotations

from ..config import AgentConfig
from ..executor import ActionOutput

PRIORITIES = ("critical", "high", "medium", "low")

RESOLVE_STATUSES = ("resolved", "closed")

#: ``task_actions.TITLE_CAP`` / ``TEXT_CAP`` on the server. Longer text is cut
#: before it is reported, so an over-long one never reaches Jackil at full
#: length. A key never comes close to either cap; it uses the text one.
TITLE_CAP = 200
TEXT_CAP = 4000


def _text(params: dict, field: str, action: str, cap: int) -> str:
    """The rendered text for *field*, stripped and capped. Refuses an empty one."""
    value = str(params.get(field) or "").strip()[:cap]
    if not value:
        raise ValueError(f"{action} needs a {field}")
    return value


def _optional_text(params: dict, field: str, cap: int) -> str:
    return str(params.get(field) or "").strip()[:cap]


def _jackil_ticket(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server opens (or re-notices) a ticket for this key.

    The priority the server applies is the one in the task it signed; checking
    it here is a save-time-quality failure report, not an authority.
    """
    title = _text(params, "title", "jackil_ticket", TITLE_CAP)
    key = _text(params, "key", "jackil_ticket", TEXT_CAP)
    priority = str(params.get("priority") or "").strip().lower()
    if priority and priority not in PRIORITIES:
        raise ValueError(
            f"jackil_ticket priority must be one of {', '.join(PRIORITIES)} "
            f"(got {priority})")
    return ActionOutput(f"Jackil ticket requested: {title} — server will open or update it",
                        {"title": title, "key": key})


def _jackil_note(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server notes this host's open ticket for the key."""
    key = _text(params, "key", "jackil_note", TEXT_CAP)
    body = _text(params, "body", "jackil_note", TEXT_CAP)
    return ActionOutput(f"Jackil note requested for {key} — server will add it",
                        {"body": body})


def _jackil_resolve(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server resolves this host's open ticket."""
    key = _text(params, "key", "jackil_resolve", TEXT_CAP)
    status = str(params.get("status") or "").strip().lower() or "resolved"
    if status not in RESOLVE_STATUSES:
        raise ValueError(
            f"jackil_resolve status must be one of {', '.join(RESOLVE_STATUSES)} "
            f"(got {status})")
    return ActionOutput(f"Jackil ticket resolve requested: {key} — server will close it",
                        {"key": key})
