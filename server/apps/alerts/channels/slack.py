"""Slack, through an incoming webhook (Slack app > Incoming Webhooks).

The webhook URL is the credential: anyone holding it can post to the channel,
so it is a ``secret_url`` and never leaves the server once saved. Mattermost
and Rocket.Chat accept the same body, so their incoming webhooks work here too.
"""
from . import ChannelType, Field, Message, post, register
from .text import defang, facts, headline

#: Slack's action id for the Acknowledge button (apps/alerts/slack_actions.py).
ACK_ACTION = "vigil_ack"
#: A button value holds 2,000 characters; 50 ids and commas fit.
MAX_ACK_IDS = 50


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
        Field("signing_secret", "Signing secret", kind="secret", required=False,
              help="Only for the Acknowledge button. In your Slack app: Basic "
                   "Information > Signing Secret. Then turn on Interactivity "
                   "and set its Request URL to <your Vigil address>"
                   "/api/v1/alerts/slack/actions/."),
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
        # The Acknowledge button rides only on a firing message whose payload
        # names alerts, and only for an app with a signing secret to check the
        # click against (apps/alerts/slack_actions.py).
        ids = message.payload.get("alert_ids") or (
            [message.payload["alert_id"]] if message.payload.get("alert_id") else [])
        if config.get("signing_secret") and message.event == "firing" and ids:
            ack = {"type": "button", "action_id": ACK_ACTION,
                   "text": {"type": "plain_text", "text": "Acknowledge"},
                   "value": ",".join(ids[:MAX_ACK_IDS])}
            if blocks[-1]["type"] == "actions":
                blocks[-1]["elements"].insert(0, ack)
            else:
                blocks.append({"type": "actions", "elements": [ack]})
        post(config["webhook_url"], json={"text": headline(message, escape), "blocks": blocks})
