"""Pushover (https://pushover.net), a push-notification service for phones and
desktop.

Vigil posts to Pushover's one API address, so this type has no URL field; the
admin supplies two credentials instead. The *user or group key* identifies
their account (or a group of people) and is shown at the top of the Pushover
dashboard; the *application token* comes from creating an application at
pushover.net/apps/build. Both are ``secret`` settings and never leave the
server once saved. A device name narrows the send to one of that account's
devices."""
from . import ChannelType, Field, Message, post, register
from .text import facts, headline

#: Pushover's single message endpoint.
API = "https://api.pushover.net/1/messages.json"

#: Pushover's priority levels as form values; -1 is lowest and 2 emergency.
#: Priority 2 is never used: it needs retry and expire settings and repeats on
#: the phone until somebody acknowledges the message.
PRIORITY = {"critical": "1", "warning": "0"}


@register
class Pushover(ChannelType):
    kind = "pushover"
    label = "Pushover"
    fields = (
        Field("user", "User or group key", kind="secret",
              help="On your Pushover dashboard."),
        Field("token", "Application token", kind="secret",
              help="Create an application at pushover.net/apps/build and copy "
                   "its token."),
        Field("device", "Device", required=False,
              help="Send to one device only. Leave blank for all."),
    )

    def send(self, config: dict, message: Message) -> None:
        lines = ([message.text] if message.text else []) + facts(message)
        body = {"token": config["token"],
                "user": config["user"],
                "title": headline(message)[:250],
                "message": ("\n".join(lines) or message.title)[:1024],
                "priority": "0" if message.event in ("resolved", "test")
                else PRIORITY.get(message.severity, "-1")}
        if message.link:
            body["url"] = message.link
            body["url_title"] = "Open in Vigil"
        device = config.get("device")
        if device:
            body["device"] = device
        post(API, data=body)
