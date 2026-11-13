"""Wording every chat and push channel shares, so they all say the same facts."""
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
