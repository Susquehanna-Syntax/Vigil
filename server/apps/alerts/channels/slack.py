"""Slack, through an incoming webhook (Slack app > Incoming Webhooks).

The webhook URL is the credential: anyone holding it can post to the channel,
so it is a ``secret_url`` and never leaves the server once saved. Mattermost
and Rocket.Chat accept the same body, so their incoming webhooks work here too.
"""
from . import ChannelType, Field, Message, post, register


def escape(text: str) -> str:
    """Slack mrkdwn treats &, < and > as markup (links, mentions)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def facts(message: Message) -> list[str]:
    """Host, site and value as short "Name: value" strings, skipping blanks."""
    out = []
    if message.host:
        out.append(f"Host: {message.host}")
    if message.site:
        out.append(f"Site: {message.site}")
    if message.value is not None:
        out.append(f"Value: {message.value}")
    if message.rule:
        out.append(f"Rule: {message.rule}")
    return out


def headline(message: Message) -> str:
    """One line every chat type leads with, and the phone notification shows."""
    return f"[{message.event.upper()}] [{message.severity.upper()}] {message.title}"


@register
class Slack(ChannelType):
    kind = "slack"
    label = "Slack"
    fields = (
        Field("webhook_url", "Webhook URL", kind="secret_url",
              help="From your Slack app's Incoming Webhooks page. It starts with "
                   "https://hooks.slack.com/services/."),
    )

    def send(self, config: dict, message: Message) -> None:
        lines = [f"*{escape(headline(message))}*"]
        if message.text:
            lines.append(escape(message.text))
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
        if facts(message):
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": escape("  |  ".join(facts(message)))}]})
        if message.link:
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Open in Vigil"},
                 "url": message.link}]})
        post(config["webhook_url"], json={"text": headline(message), "blocks": blocks})
