"""An "Open a ticket" channel: a POST to any ticketing system's create-issue URL.

Jira, Zammad, GLPI and most PSA tools create a ticket from a JSON POST, but each
one wants its own field names. So the request body is the admin's own JSON, with
``{{placeholders}}`` (title, text, host, site, severity, event, value, rule,
link) that Vigil fills from the alert; a blank body sends a sensible default.
Every value is JSON-escaped as it is filled in, so a quote or newline coming
from an agent cannot break the string it lands in, and an address is checked and
a token kept exactly as every other channel does."""
import json

from . import ChannelError, ChannelType, Field, Message, post, register
from .text import facts, headline

#: Every ``{{name}}`` a body may use.
PLACEHOLDERS = ("title", "text", "host", "site", "severity", "event", "value", "rule", "link")

DEFAULT_BODY = (
    '{"title": "{{title}}", "description": "{{text}}", "host": "{{host}}", '
    '"site": "{{site}}", "severity": "{{severity}}", "event": "{{event}}", '
    '"link": "{{link}}"}'
)


def _values(message) -> dict:
    return {
        "title": headline(message),
        "text": "\n".join(([message.text] if message.text else []) + facts(message))
                or message.title,
        "host": message.host,
        "site": message.site,
        "severity": message.severity,
        "event": message.event,
        "value": "" if message.value is None else f"{message.value:g}",
        "rule": message.rule,
        "link": message.link,
    }


def render(template: str, message) -> dict:
    """The admin's template with every {{name}} replaced by its value.

    Each value is JSON-escaped as it goes in, so a quote or a newline coming
    from an alert cannot break out of the string it sits in. Raises
    ``ChannelError`` when the filled-in result is not JSON: the admin wrote the
    template, so that is their mistake to read about, not something to send on.
    """
    values = _values(message)
    body = template or DEFAULT_BODY
    for name in PLACEHOLDERS:
        escaped = json.dumps(str(values[name]))[1:-1]
        body = body.replace("{{" + name + "}}", escaped)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ChannelError(
            f"The request body is not valid JSON after filling it in: {exc}"
        ) from exc


@register
class Ticket(ChannelType):
    kind = "ticket"
    label = "Open a ticket"
    fields = (
        Field("url", "Create-issue URL", kind="url",
              help="Your ticketing system's API address for a new ticket."),
        Field("auth_header", "Authorization header", kind="secret", required=False,
              help='Sent as the Authorization header, for example "Bearer <token>" or '
                   '"Basic <base64>".'),
        Field("body", "Request body", kind="json", required=False,
              help="JSON with {{title}}, {{text}}, {{host}}, {{site}}, {{severity}}, "
                   "{{event}}, {{value}}, {{rule}}, {{link}}. "
                   "Leave blank for a default body."),
    )

    def send(self, config: dict, message: Message) -> None:
        headers = {"Content-Type": "application/json"}
        if config.get("auth_header"):
            headers["Authorization"] = config["auth_header"]
        post(config["url"], json=render(config.get("body", ""), message), headers=headers)
