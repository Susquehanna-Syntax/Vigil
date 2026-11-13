"""Wording every chat and push channel shares, so they all say the same facts."""
import re

from . import Message


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


#: Mentions that notify a whole room on Slack-compatible chats and Discord.
_MASS_MENTIONS = re.compile(r"@(channel|all|here|everyone)\b", re.IGNORECASE)
_ZWSP = "​"


def defang(text: str) -> str:
    """Text from an alert, made unable to ping a room or disguise a link.

    Host names, rule names and messages come from agents and admins, so a host
    called ``@channel`` or a message holding ``[login](https://evil)`` must
    reach the chat as plain text. An invisible zero-width space after the @ and
    between ``]`` and ``(`` breaks both without changing what people read.
    """
    text = _MASS_MENTIONS.sub(lambda m: "@" + _ZWSP + m.group(1), text)
    return text.replace("](", "]" + _ZWSP + "(")
