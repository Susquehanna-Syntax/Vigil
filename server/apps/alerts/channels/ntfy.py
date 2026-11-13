"""ntfy (https://ntfy.sh), a self-hostable push-notification service.

The admin publishes to a *topic* on their own ntfy server, or on the public
ntfy.sh; the phone app subscribes to that topic and shows the notification. The
server URL is the address of the ntfy server, and an access token (tk_..., from
that server's Access tokens page) is needed only when the server requires a
login, so it is a ``secret`` and never leaves the server once saved. The public
service runs at https://ntfy.sh, which is the field's default."""
import re

from . import ChannelError, ChannelType, Field, Message, post, register
from .text import facts, headline

#: ntfy's own priority levels; the phone shows 3 and above as an alert.
PRIORITY = {"critical": 5, "warning": 4}

#: What a topic may be called, straight from ntfy's documentation.
TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@register
class Ntfy(ChannelType):
    kind = "ntfy"
    label = "ntfy"
    fields = (
        Field("server", "Server", kind="url", default="https://ntfy.sh",
              help="Your own ntfy server, or leave it as ntfy.sh."),
        Field("topic", "Topic", help="Letters, digits, - and _ only. On ntfy.sh anyone who "
              "knows the topic can read it, so pick one nobody will guess."),
        Field("token", "Access token", kind="secret", required=False,
              help="Only if the server requires a login (tk_...)."),
    )

    def send(self, config: dict, message: Message) -> None:
        topic = config["topic"]
        if not TOPIC.match(topic):
            raise ChannelError("the topic may hold only letters, digits, - and _")
        lines = ([message.text] if message.text else []) + facts(message)
        body = {"topic": topic,
                "title": headline(message),
                "message": "\n".join(lines) or message.title,
                "priority": 3 if message.event in ("resolved", "test")
                else PRIORITY.get(message.severity, 3)}
        if message.link:
            body["click"] = message.link
        headers = {"Authorization": f"Bearer {config['token']}"} if config.get("token") else {}
        post(config["server"].rstrip("/") + "/", json=body, headers=headers)
