"""Slack's Interactivity Request URL: the Acknowledge button on an alert.

Slack signs every click with the app's signing secret (HMAC-SHA256 over
"v0:<timestamp>:<raw body>", sent as X-Slack-Signature "v0=<hex>"). The click is
trusted only when it matches the signing secret of an enabled Slack channel, and
acknowledges only alerts that channel was allowed to hear about.

No person is behind a Slack click, so the whole view runs in a system scope: the
matched channel's signing secret is what says the request is real.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid

from django.utils.timezone import now
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from vigil import scoping

from . import channels
from .api_schema import AlertErrorSerializer
from .models import Alert, NotificationChannel

logger = logging.getLogger(__name__)

#: Slack rejects a replay older than five minutes; so do we.
MAX_SKEW_SECONDS = 300
#: The one button this endpoint answers; channels/slack.py builds it.
ACK_ACTION = "vigil_ack"
#: A button value holds at most 50 ids, the cap in channels/slack.py.
MAX_ACK_IDS = channels.slack.MAX_ACK_IDS

_NO_PAYLOAD = object()


@scoping.system_view(reason="Slack acknowledge: the channel's signing secret is the access control")
@extend_schema(request=None, responses={200: None, 401: AlertErrorSerializer})
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def slack_action(request):
    # request.body first: DRF refuses it once the parsers have run, and the
    # signature is over these exact bytes.
    raw = request.body
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not _fresh(ts):
        return Response({"error": "Stale or missing timestamp"}, status=401)

    # The signature before anything reads the body: an unsigned caller learns
    # nothing about what a well-formed click looks like.
    channel = _signed_channel(raw, ts, request.headers.get("X-Slack-Signature", ""))
    if channel is None:
        return Response({"error": "Bad signature"}, status=401)
    click = _click(raw)
    if click is _NO_PAYLOAD:
        return Response({"error": "No payload"}, status=400)

    action, response_url, who = click
    if action is None:
        return Response({})
    count = _acknowledge(channel, _alert_ids(action.get("value", "")), who)
    # The name came from Slack, and goes into a Slack message: escape it.
    _reply(response_url, channels.slack.escape(who), count)
    return Response({})


def _fresh(ts: str) -> bool:
    """Slack's own freshness check, so a captured click cannot be replayed."""
    try:
        seconds = int(ts)
    except ValueError:
        return False
    return abs(time.time() - seconds) <= MAX_SKEW_SECONDS


def _signed_channel(raw: bytes, ts: str, signature: str):
    """The enabled Slack channel whose signing secret signs this body."""
    if not signature.startswith("v0="):
        return None
    message = b"v0:" + ts.encode() + b":" + raw
    with scoping.system(reason="Slack acknowledge: finding the channel whose signing "
                               "secret signed this click"):
        for channel in NotificationChannel.objects.filter(kind="slack", enabled=True):
            try:
                secret = channels.plain_config(channels.get("slack"),
                                               channel.config).get("signing_secret")
            except channels.ChannelError:
                # One channel whose secrets no longer decrypt must not refuse
                # the clicks every other channel signs.
                logger.warning("Slack channel %s: a stored secret could not be "
                               "decrypted", channel.pk)
                continue
            if not secret:
                continue
            digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
            if hmac.compare_digest(f"v0={digest}", signature):
                return channel
    return None


def _click(raw: bytes):
    """``(action, response_url, user)``, or ``_NO_PAYLOAD`` for a body Slack
    never sent. ``(None, ...)`` when the payload holds no Acknowledge action."""
    try:
        form = urllib.parse.parse_qs(raw.decode())
        payload = json.loads(form["payload"][0])
    except (ValueError, KeyError, IndexError, UnicodeDecodeError):
        return _NO_PAYLOAD
    if not isinstance(payload, dict):
        return _NO_PAYLOAD
    response_url = payload.get("response_url") or ""
    user = payload.get("user")
    who = (user.get("username") or user.get("name") or "someone"
           if isinstance(user, dict) else "someone")
    action = next((a for a in payload.get("actions", [])
                   if isinstance(a, dict) and a.get("action_id") == ACK_ACTION), None)
    return action, response_url, who


def _alert_ids(value: str) -> list[str]:
    """The button's value split into ids, dropping what is not one."""
    out = []
    for part in value.split(","):
        try:
            out.append(str(uuid.UUID(part)))
        except ValueError:
            continue
        if len(out) >= MAX_ACK_IDS:
            break
    return out


def _acknowledge(channel, ids: list[str], who: str) -> int:
    """Acknowledge the firing alerts *ids* names that *channel* may hear."""
    count = 0
    with scoping.system(reason="Slack acknowledge button, signature checked "
                               "against the channel"):
        for alert_id in ids:
            alert = (Alert.objects.filter(pk=alert_id, state=Alert.State.FIRING)
                     .select_related("host").first())
            if alert is None or not _channel_hears(channel, alert):
                continue
            alert.state = Alert.State.ACKNOWLEDGED
            alert.acknowledged_at = now()
            alert.acknowledged_until = None
            alert.save(update_fields=["state", "acknowledged_at", "acknowledged_until"])
            count += 1
    logger.info("%d alert(s) acknowledged from Slack by %s via channel %s",
                count, who, channel.pk)
    return count


def _channel_hears(channel, alert) -> bool:
    """Exactly what notifications._channels_for_host decides."""
    scoping.prime_site_ids([alert.host])
    return scoping.within_site(channel, alert.host)


def _reply(response_url, who: str, count: int) -> None:
    """Say in the thread what the click did. A failed reply must not undo it."""
    parts = urllib.parse.urlsplit(response_url) if isinstance(response_url, str) else None
    if parts is None or parts.scheme != "https" or parts.hostname != "hooks.slack.com":
        return
    if count:
        more = f" ({count} alerts)" if count > 1 else ""
        text = f"Acknowledged in Vigil by {who}{more}"
    else:
        text = "Nothing to acknowledge: already acknowledged or resolved"
    try:
        channels.post(response_url, json={"replace_original": False,
                                          "response_type": "in_channel",
                                          "text": text})
    except channels.ChannelError as exc:
        logger.warning("could not answer the Slack acknowledgement: %s", exc)
