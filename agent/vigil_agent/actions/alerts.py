"""raise_alert / resolve_alert / send_notification — marker actions (2026.14.6).

None of these touch this machine: an alert and a notification are server-side
state. The agent reports the step and the server acts on it when the result
arrives, taking severity, key and channel from the task it signed rather than
from anything reported here.

Params arrive already rendered by the runtime (``runtime.resolve_params``), so
``params["message"]`` is the text an operator will read. It is capped here at
the same length the server caps it at, so the reported text and the stored one
agree.
"""

from __future__ import annotations

from ..config import AgentConfig
from ..executor import ActionOutput

SEVERITIES = ("info", "warning", "critical")

#: ``task_actions.MESSAGE_CAP`` on the server. A longer message is cut before it
#: is reported, so an over-long one never reaches a channel at full length.
MESSAGE_CAP = 2000


def _message(params: dict, action: str) -> str:
    """The rendered message, stripped and capped. Refuses an empty one."""
    message = str(params.get("message") or "").strip()[:MESSAGE_CAP]
    if not message:
        raise ValueError(f"{action} needs a message")
    return message


def _raise_alert(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server opens an alert on this host.

    The severity the server applies is the one in the task it signed; checking
    it here is a save-time-quality failure report, not an authority.
    """
    severity = str(params.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        raise ValueError(
            f"raise_alert needs severity of {', '.join(SEVERITIES)} "
            f"(got {severity or 'nothing'})")
    message = _message(params, "raise_alert")
    return ActionOutput(f"Alert requested: {severity}: {message} — server will raise it",
                        {"message": message})


def _resolve_alert(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server resolves this host's alert with that key."""
    key = str(params.get("key") or "").strip()
    if not key:
        raise ValueError("resolve_alert needs the key of the alert to resolve")
    return ActionOutput(f"Alert resolve requested: {key} — server will resolve it",
                        {"key": key})


def _send_notification(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server sends a message. Creates no alert."""
    message = _message(params, "send_notification")
    return ActionOutput("Notification requested — server will send it",
                        {"message": message})
