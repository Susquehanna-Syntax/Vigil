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
import contextvars
import ipaddress
import json as jsonlib
import socket
import urllib.parse
from dataclasses import dataclass, field

import certifi
import urllib3

#: Prefix of an encrypted setting inside NotificationChannel.config.
ENCRYPTED_PREFIX = "enc:"
#: Field kinds stored encrypted and never returned by the API.
SECRET_KINDS = ("secret", "secret_url")
#: Seconds any channel waits for a remote server.
TIMEOUT_SECONDS = 10


class ChannelError(Exception):
    """A delivery failed. The message is short and safe to show an admin."""


@dataclass(frozen=True)
class Field:
    """One setting of a channel type, as the API and the form present it.

    ``kind`` is ``text``, ``url``, ``secret`` (encrypted at rest, never read
    back), ``secret_url`` (a URL that carries its own token, as chat webhooks
    do: checked like a ``url``, stored like a ``secret``, and shown only as
    its scheme and host), ``list`` (strings; a comma or newline separated
    string is split), or ``choice`` (one of ``choices``).
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


def _stored_plain(value) -> str:
    """A stored secret's plaintext, or "" when there is none or it is unreadable."""
    try:
        return _decrypt(value) if value else ""
    except ChannelError:
        return ""


def clean_config(channel_type: ChannelType, raw, existing: dict | None = None):
    """``(config, errors)``: *raw* checked against the type's fields, secrets
    encrypted, unknown keys dropped. A secret sent blank keeps the stored one,
    so editing a channel never needs its token again."""
    existing = existing or {}
    if not isinstance(raw, dict):
        return {}, {"config": "must be an object"}
    # A stored secret is kept only while every address stays the same: an
    # admin who may edit a channel but never saw its token must not be able to
    # point the URL at their own server and have Vigil send the token there.
    urls_unchanged = all(
        str(raw.get(f.name) or "").strip() == str(existing.get(f.name) or "")
        for f in channel_type.fields if f.kind == "url") and not any(
        isinstance(raw.get(f.name), str) and raw[f.name].strip()
        and raw[f.name].strip() != _stored_plain(existing.get(f.name))
        for f in channel_type.fields if f.kind == "secret_url")
    config, errors = {}, {}
    for f in channel_type.fields:
        value = raw.get(f.name, None)
        if f.kind == "secret_url" and value not in (None, "") and (
                not isinstance(value, str)
                or not value.strip().lower().startswith(("http://", "https://"))):
            errors[f.name] = "must start with http:// or https://"
            continue
        if f.kind in SECRET_KINDS:
            if value in (None, ""):
                if existing.get(f.name) and urls_unchanged:
                    config[f.name] = existing[f.name]
                elif f.required or existing.get(f.name):
                    errors[f.name] = ("enter it again: the address changed" if existing.get(f.name)
                                      else "required")
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
        if f.kind in SECRET_KINDS and out.get(f.name):
            out[f.name] = _decrypt(out[f.name])
    return out


def public_config(channel_type: ChannelType | None, config: dict,
                  *, full: bool = True) -> tuple[dict, list[str]]:
    """``(config, secrets_set)`` for the API: secret values left out, and the
    names of the secrets that are set listed instead.

    Without *full* (a caller who may see the channel but not change it, such
    as a site admin looking at a global channel) a URL shows only its scheme
    and host: chat webhook URLs carry their token in the path.
    """
    if channel_type is None:
        return {}, []
    secret_names = {f.name for f in channel_type.fields if f.kind in SECRET_KINDS}
    url_names = {f.name for f in channel_type.fields if f.kind == "url"}
    shown = {}
    for f in channel_type.fields:
        # Scheme and host only, so the list can say where a chat channel posts.
        if f.kind == "secret_url" and (config or {}).get(f.name):
            try:
                parts = urllib.parse.urlsplit(_decrypt(config[f.name]))
                shown[f.name] = f"{parts.scheme}://{parts.hostname or ''}/..."
            except (ChannelError, ValueError):
                pass
    for key, value in (config or {}).items():
        if key in secret_names:
            continue
        if key in url_names and not full and isinstance(value, str):
            parts = urllib.parse.urlsplit(value)
            value = f"{parts.scheme}://{parts.hostname or ''}/..."
        shown[key] = value
    return shown, sorted(n for n in secret_names if (config or {}).get(n))


# ── HTTP ────────────────────────────────────────────────────────────────────

#: Whether the channel being sent through may reach private (LAN) addresses.
#: Set by notifications.deliver from the channel row for the length of a send.
allow_private = contextvars.ContextVar("channel_allow_private", default=False)


def _resolve(host: str, port: int) -> list[str]:
    """Every address *host* resolves to (a seam for tests)."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [info[4][0].split("%", 1)[0] for info in infos]


def check_destination(url: str, *, private_ok: bool) -> str:
    """The address to connect to for *url*, or ChannelError.

    Never: loopback, link-local (which holds the cloud metadata address),
    unspecified, multicast or reserved addresses, so no admin can turn a
    channel into a way to make Vigil call itself. Private LAN addresses only
    when *private_ok*: a homelab's ntfy or Gotify lives there, so an admin of
    the whole install may choose it, but a site admin may not. Every address
    the name resolves to is checked; the first is returned, and the request is
    sent to that address, so DNS changing its answer in between cannot move it.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ChannelError("the address must be an http:// or https:// URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = _resolve(parts.hostname, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise ChannelError(f"{parts.hostname} does not resolve") from exc
    if not addresses:
        raise ChannelError(f"{parts.hostname} does not resolve")
    for text in addresses:
        for a in _with_embedded(ipaddress.ip_address(text)):
            if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast \
                    or a.is_reserved:
                raise ChannelError(f"{parts.hostname} resolves to {a}, which Vigil will not send to")
            # Not a denylist: without LAN permission the address must be
            # globally routable, which also rules out carrier-grade NAT
            # (100.64.0.0/10, where Tailscale lives) and documentation ranges.
            if not private_ok and not a.is_global:
                raise ChannelError(
                    f"{parts.hostname} is on a private network: only an administrator of the "
                    f"whole install can send there")
    return addresses[0]


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _with_embedded(address):
    """*address* and every IPv4 address an IPv6 one carries inside it
    (mapped, 6to4, Teredo, NAT64): each reaches that IPv4 host, so each is
    checked as if it were the destination."""
    out = [address]
    if address.version == 6:
        if address.ipv4_mapped:
            out.append(address.ipv4_mapped)
        if address.sixtofour:
            out.append(address.sixtofour)
        if address.teredo:
            out.extend(address.teredo)
        if any(address in net for net in _NAT64):
            out.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return out


def _transport(method, scheme, address, port, hostname, path, body, headers):
    """One request to *address*, presenting *hostname* for TLS and Host (a
    seam for tests). Returns the status code; the body is never read."""
    kwargs = {"timeout": urllib3.Timeout(total=TIMEOUT_SECONDS), "retries": False, "maxsize": 1}
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            address, port, server_hostname=hostname, assert_hostname=hostname,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), **kwargs)
    else:
        # Plain HTTP only when the admin entered an http:// URL, as a LAN ntfy
        # or Gotify often is; the address was still checked by check_destination.
        pool = urllib3.HTTPConnectionPool(address, port, **kwargs)  # nosemgrep: http-not-https-connection
    try:
        resp = pool.urlopen(method, path, body=body, headers=headers,
                            redirect=False, preload_content=False)
        status = resp.status
        resp.release_conn()
        return status
    finally:
        pool.close()


def post(url: str, *, json=None, data=None, headers=None) -> None:
    """POST to a channel's endpoint, or raise ChannelError with the reason.

    Redirects are not followed: a channel URL that answers with one is
    misconfigured, and following it could carry the message (and its token
    header) somewhere the admin never entered.
    """
    address = check_destination(url, private_ok=allow_private.get())
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    head = {"Host": parts.hostname + (f":{parts.port}" if parts.port else ""),
            "User-Agent": "Vigil"}
    if json is not None:
        body = jsonlib.dumps(json).encode()
        head["Content-Type"] = "application/json"
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        head["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        body = data.encode() if isinstance(data, str) else data
    head.update(headers or {})
    try:
        status = _transport("POST", parts.scheme, address, port, parts.hostname, path, body, head)
    except urllib3.exceptions.TimeoutError as exc:
        raise ChannelError(f"no answer within {TIMEOUT_SECONDS} seconds") from exc
    except urllib3.exceptions.SSLError as exc:
        raise ChannelError("its TLS certificate did not verify") from exc
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise ChannelError("could not connect") from exc
    if not 200 <= status < 300:
        raise ChannelError(f"the server answered HTTP {status}")


# Channel modules register themselves on import. F401: the names appear nowhere
# else, only their import registers each channel type. Wrapping the line for its
# length left nothing for E402 to cover, since a channel module may never import
# this one at module level anyway - it is what they all import from.
from . import (  # noqa: F401
    discord,
    email,
    gotify,
    ntfy,
    pushover,
    slack,
    teams,
    webhook,
)
