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


def headline(message: Message, clean=str) -> str:
    """One line every chat type leads with, and the phone notification shows.

    *clean* (``defang``, or a chat's own escaping) is applied to the title only,
    the part that comes from the alert; the bracketed event and severity are
    Vigil's own and stay as they are.
    """
    return f"[{message.event.upper()}] [{message.severity.upper()}] {clean(message.title)}"


#: Mentions that notify a whole room on Slack-compatible chats and Discord.
_MASS_MENTIONS = re.compile(r"@(channel|all|here|everyone)\b", re.IGNORECASE)
_ZWSP = "​"


def defang(text: str) -> str:
    """Text from an alert, made unable to ping a room or disguise a link.

    Host names, rule names and messages come from agents and admins, so a host
    called ``@channel`` or a message holding ``[login](https://evil)`` must
    reach the chat as plain text. An invisible zero-width space after the @
    breaks a mention, and one after every ``]`` breaks every Markdown link form:
    inline ``[a](b)``, reference ``[a][b]`` and ``[a][]``, and the ``[b]: url``
    definition a shortcut ``[b]`` needs, since each relies on the character
    straight after a ``]``. People read the same text.
    """
    text = _MASS_MENTIONS.sub(lambda m: "@" + _ZWSP + m.group(1), text)
    return text.replace("]", "]" + _ZWSP)
