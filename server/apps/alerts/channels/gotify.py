"""Gotify (https://gotify.net), a self-hosted push-notification server.

The admin runs their own Gotify and points Vigil at it; the phone app or web
UI subscribes to whatever arrives. The application token comes from Gotify's
Apps > Create Application page, and is sent in the X-Gotify-Key header rather
than the URL so it never lands in a proxy log: it is a ``secret`` and never
leaves the server once saved. A server behind a path prefix works too, since
the message address is built from what the admin entered."""
from . import ChannelType, Field, Message, post, register
from .text import facts, headline

#: Gotify's priority levels; 4 and above shows a notification on the client.
PRIORITY = {"critical": 8, "warning": 5}


@register
class Gotify(ChannelType):
    kind = "gotify"
    label = "Gotify"
    fields = (
        Field("server", "Server", kind="url",
              help="Your Gotify server, such as https://gotify.example.com."),
        Field("token", "Application token", kind="secret",
              help="Apps > Create Application in Gotify, then copy its token."),
    )

    def send(self, config: dict, message: Message) -> None:
        lines = ([message.text] if message.text else []) + facts(message)
        body = {"title": headline(message),
                "message": "\n".join(lines) or message.title,
                "priority": 2 if message.event in ("resolved", "test")
                else PRIORITY.get(message.severity, 2)}
        if message.link:
            body["extras"] = {"client::notification": {"click": {"url": message.link}}}
        post(config["server"].rstrip("/") + "/message", json=body,
             headers={"X-Gotify-Key": config["token"]})
