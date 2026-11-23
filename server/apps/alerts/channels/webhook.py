"""The generic webhook: the JSON body Vigil has always sent, unchanged."""
from . import ChannelType, Field, Message, post, register


@register
class Webhook(ChannelType):
    kind = "webhook"
    label = "Webhook"
    fields = (
        Field("url", "URL", kind="url"),
        Field("secret", "Shared secret", kind="secret", required=False,
              help="Sent as the X-Vigil-Secret header, so the receiver can check the sender."),
    )

    def send(self, config: dict, message: Message) -> None:
        headers = {"Content-Type": "application/json"}
        if config.get("secret"):
            headers["X-Vigil-Secret"] = config["secret"]
        post(config["url"], json=message.payload, headers=headers)
