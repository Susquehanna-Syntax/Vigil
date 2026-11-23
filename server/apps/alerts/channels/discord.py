"""Discord, through a channel webhook (Discord: Server Settings > Integrations
> Webhooks).

The webhook URL is the credential: anyone holding it can post to the channel,
so it is a ``secret_url`` and never leaves the server once saved. The alert is
one embed under a plain text line, with mentions switched off so an alert
about a host called ``@everyone`` cannot ping the room."""
from . import ChannelType, Field, Message, post, register
from .text import defang, facts, headline

#: Discord's own limits on an embed.
TITLE_CHARS = 256
DESCRIPTION_CHARS = 4000


@register
class Discord(ChannelType):
    kind = "discord"
    label = "Discord"
    fields = (
        Field("webhook_url", "Webhook URL", kind="secret_url",
              help="Server Settings > Integrations > Webhooks > Copy Webhook URL."),
    )

    def send(self, config: dict, message: Message) -> None:
        embed = {"title": defang(message.title)[:TITLE_CHARS]}
        if message.text:
            embed["description"] = defang(message.text)[:DESCRIPTION_CHARS]
        if message.link:
            embed["url"] = message.link
        embed["fields"] = [{"name": name, "value": defang(value), "inline": True}
                           for name, value in
                           (f.split(": ", 1) for f in facts(message))]
        post(config["webhook_url"], json={"content": headline(message, defang),
                                          "allowed_mentions": {"parse": []},
                                          "embeds": [embed]})
