"""Microsoft Teams, through a workflow (Teams: the channel's ... menu >
Workflows > "Post to a channel when a webhook request is received").

The old Office 365 connectors are retired, so the URL is the one Power Automate
gives you when it creates that workflow, and it accepts an Adaptive Card. The
URL is the credential: anyone holding it can post to the channel, so it is a
``secret_url`` and never leaves the server once saved. The card carries no
colour, accent or container style at all, so an alert cannot look more urgent
than Vigil says it is."""
from . import ChannelType, Field, Message, post, register
from .text import defang, facts, headline


@register
class Teams(ChannelType):
    kind = "teams"
    label = "Microsoft Teams"
    fields = (
        Field("webhook_url", "Workflow URL", kind="secret_url",
              help="In Teams: the channel's ... menu > Workflows > \"Post to a channel when "
                   "a webhook request is received\", then copy the URL it gives you."),
    )

    def send(self, config: dict, message: Message) -> None:
        blocks = [{"type": "TextBlock", "text": headline(message, defang),
                   "weight": "Bolder", "wrap": True}]
        if message.text:
            blocks.append({"type": "TextBlock", "text": defang(message.text), "wrap": True})
        if facts(message):
            blocks.append({"type": "FactSet", "facts": [
                {"title": name, "value": defang(value)}
                for name, value in (f.split(": ", 1) for f in facts(message))]})
        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": blocks}
        if message.link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Vigil",
                                "url": message.link}]
        post(config["webhook_url"], json={"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }]})
