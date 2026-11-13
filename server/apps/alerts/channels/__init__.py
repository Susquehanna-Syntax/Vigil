"""Notification channel types (sprint M7).

A channel type is a class that names the settings it needs (``fields``) and
knows how to deliver one ``Message`` (``send``). Everything else is generic and
lives here: the registry, config validation, encryption of secret settings at
rest, and the shared HTTP helper. The channels API and the Settings form are
both built from ``fields``, so a new channel is one module in this package plus
its test, with no view or UI work.

To add one: subclass ``ChannelType``, set ``kind``, ``label`` and ``fields``,
implement ``send``, decorate the class with ``@register``, and import the module
at the bottom of this file. ``send`` raises ``ChannelError`` with a short reason
on failure; it never returns a remote server's response body, which the "Send
test" button would otherwise turn into a way to read internal pages.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

import requests

#: Prefix of an encrypted setting inside NotificationChannel.config.
ENCRYPTED_PREFIX = "enc:"
#: Seconds any channel waits for a remote server.
TIMEOUT_SECONDS = 10


class ChannelError(Exception):
    """A delivery failed. The message is short and safe to show an admin."""


@dataclass(frozen=True)
class Field:
    """One setting of a channel type, as the API and the form present it.

    ``kind`` is ``text``, ``url``, ``secret`` (encrypted at rest, never read
    back), ``list`` (strings; a comma or newline separated string is split),
    or ``choice`` (one of ``choices``).
    """

    name: str
    label: str
    kind: str = "text"
    required: bool = True
    help: str = ""
    choices: tuple[str, ...] = ()
    default: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "required": self.required, "help": self.help,
                "choices": list(self.choices), "default": self.default}


@dataclass
class Message:
    """One notification, already worded, for any channel to format.

    ``payload`` is the JSON body the generic webhook has always sent; it is
    kept byte for byte so existing receivers do not break.
    """

    event: str  # "firing", "resolved" or "test"
    severity: str  # "info", "warning" or "critical"
    title: str
    text: str
    host: str = ""
    site: str = ""
    value: float | None = None
    rule: str = ""
    link: str = ""
    payload: dict = field(default_factory=dict)


class ChannelType:
    kind: str = ""
    label: str = ""
    fields: tuple[Field, ...] = ()
    #: A licensing feature the type needs, or "" for every edition.
    feature: str = ""

    def send(self, config: dict, message: Message) -> None:  # pragma: no cover
        raise NotImplementedError

    def as_dict(self) -> dict:
        from vigil import licensing

        return {"kind": self.kind, "label": self.label,
                "fields": [f.as_dict() for f in self.fields],
                "feature": self.feature,
                "available": not self.feature or licensing.has_feature(self.feature)}


_REGISTRY: dict[str, ChannelType] = {}


def register(cls):
    """Class decorator: make a channel type available by its ``kind``."""
    if not cls.kind or cls.kind in _REGISTRY:
        raise ValueError(f"channel kind {cls.kind!r} is empty or registered twice")
    _REGISTRY[cls.kind] = cls()
    return cls


def get(kind: str) -> ChannelType | None:
    return _REGISTRY.get(kind)


def all_types() -> list[ChannelType]:
    return sorted(_REGISTRY.values(), key=lambda t: t.label.lower())


# ── Config: validation, encryption, presentation ────────────────────────────

def _encrypt(value: str) -> str:
    from apps.hosts.crypto import encrypt_secret

    return ENCRYPTED_PREFIX + base64.b64encode(encrypt_secret(value)).decode()


def _decrypt(value: str) -> str:
    """The plaintext of a stored secret. A value written before secrets were
    encrypted is returned as it is, so old webhook rows keep working."""
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        return value
    from apps.hosts.crypto import decrypt_secret

    try:
        return decrypt_secret(base64.b64decode(value[len(ENCRYPTED_PREFIX):]))
    except (binascii.Error, ValueError) as exc:
        raise ChannelError("a stored secret could not be decrypted") from exc


def clean_config(channel_type: ChannelType, raw, existing: dict | None = None):
    """``(config, errors)``: *raw* checked against the type's fields, secrets
    encrypted, unknown keys dropped. A secret sent blank keeps the stored one,
    so editing a channel never needs its token again."""
    existing = existing or {}
    if not isinstance(raw, dict):
        return {}, {"config": "must be an object"}
    config, errors = {}, {}
    for f in channel_type.fields:
        value = raw.get(f.name, None)
        if f.kind == "secret":
            if value in (None, ""):
                if existing.get(f.name):
                    config[f.name] = existing[f.name]
                elif f.required:
                    errors[f.name] = "required"
                continue
            if not isinstance(value, str) or len(value) > 2000:
                errors[f.name] = "must be text of at most 2000 characters"
                continue
            config[f.name] = _encrypt(value.strip())
            continue
        if f.kind == "list":
            if isinstance(value, str):
                value = [v for v in (p.strip() for p in value.replace("\n", ",").split(",")) if v]
            if value is None:
                value = []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value) \
                    or len(value) > 50:
                errors[f.name] = "must be a list of at most 50 strings"
                continue
            value = [v.strip()[:255] for v in value if v.strip()]
            if f.required and not value:
                errors[f.name] = "required"
                continue
            config[f.name] = value
            continue
        value = "" if value is None else value
        if not isinstance(value, str) or len(value) > 2000:
            errors[f.name] = "must be text of at most 2000 characters"
            continue
        value = value.strip() or f.default
        if not value:
            if f.required:
                errors[f.name] = "required"
            continue
        if f.kind == "url" and not value.lower().startswith(("http://", "https://")):
            errors[f.name] = "must start with http:// or https://"
            continue
        if f.kind == "choice" and value not in f.choices:
            errors[f.name] = f"must be one of {', '.join(f.choices)}"
            continue
        config[f.name] = value
    return config, errors


def plain_config(channel_type: ChannelType, config: dict) -> dict:
    """The config with secrets decrypted, for ``send`` only."""
    out = dict(config or {})
    for f in channel_type.fields:
        if f.kind == "secret" and out.get(f.name):
            out[f.name] = _decrypt(out[f.name])
    return out


def public_config(channel_type: ChannelType | None, config: dict) -> tuple[dict, list[str]]:
    """``(config, secrets_set)`` for the API: secret values left out, and the
    names of the secrets that are set listed instead."""
    if channel_type is None:
        return {}, []
    secret_names = {f.name for f in channel_type.fields if f.kind == "secret"}
    shown = {k: v for k, v in (config or {}).items() if k not in secret_names}
    return shown, sorted(n for n in secret_names if (config or {}).get(n))


# ── HTTP ────────────────────────────────────────────────────────────────────

def post(url: str, *, json=None, data=None, headers=None) -> None:
    """POST to a channel's endpoint, or raise ChannelError with the reason.

    Redirects are not followed: a channel URL that answers with one is
    misconfigured, and following it could carry the message (and its token
    header) somewhere the admin never entered.
    """
    try:
        resp = requests.post(url, json=json, data=data, headers=headers or {},
                             timeout=TIMEOUT_SECONDS, allow_redirects=False)
    except requests.Timeout as exc:
        raise ChannelError(f"no answer within {TIMEOUT_SECONDS} seconds") from exc
    except requests.RequestException as exc:
        raise ChannelError("could not connect") from exc
    if not 200 <= resp.status_code < 300:
        raise ChannelError(f"the server answered HTTP {resp.status_code}")


# Channel modules register themselves on import.
from . import email, webhook  # noqa: E402,F401
