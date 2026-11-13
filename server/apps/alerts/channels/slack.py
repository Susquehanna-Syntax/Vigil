"""Slack, through an incoming webhook (Slack app > Incoming Webhooks).

The webhook URL is the credential: anyone holding it can post to the channel,
so it is a ``secret_url`` and never leaves the server once saved. Mattermost
and Rocket.Chat accept the same body, so their incoming webhooks work here too.
"""
from . import ChannelType, Field, Message, post, register
from .text import defang, facts, headline


def escape(text: str) -> str:
    """Slack mrkdwn treats &, < and > as markup (``<!channel>``, links), and
    Mattermost honours plain ``@channel`` and Markdown links: neither may come
    through from an alert."""
    return defang(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        lines = [f"*{headline(message, escape)}*"]
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
        post(config["webhook_url"], json={"text": headline(message, escape), "blocks": blocks})
