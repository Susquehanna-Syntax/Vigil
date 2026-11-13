"""Notification channels API (sprint M7).

Admins manage channels here instead of the Django admin. The form for each
channel type is built from its fields (apps.alerts.channels), so this module
does not change when a type is added. Site rules are the playbooks' ones: a
global channel is an instance admin's, a site admin manages their own site's,
and every admin sees the global channels that also notify their site.
"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from apps.accounts.permissions import IsAdmin
from vigil import licensing, scoping
from vigil.licensing import upgrade_body

from . import channels
from .channel_schema import (
    AlertChannelSerializer,
    AlertChannelTestSerializer,
    AlertChannelTypeSerializer,
    AlertChannelWriteSerializer,
)
from .models import NotificationChannel
from .notifications import deliver

_NAME_UNIQUE = "channel names are unique across the install: tasks name a channel to notify"


class _TestSendThrottle(UserRateThrottle):
    """A test send reaches out to a remote server; keep a slip of the mouse
    (or a script) from turning the button into a flood."""

    scope = "channel_test"
    rate = "10/min"


def _visible(user):
    return scoping.with_sites(scoping.filter_by_site(
        NotificationChannel.objects.all(), user, cascade_global=True))


def _row(channel: NotificationChannel) -> dict:
    kind = channels.get(channel.kind)
    config, secrets_set = channels.public_config(kind, channel.config)
    return {
        "id": str(channel.id),
        "name": channel.name,
        "kind": channel.kind,
        "kind_label": kind.label if kind else channel.kind,
        "config": config,
        "secrets_set": secrets_set,
        "on_firing": channel.on_firing,
        "on_resolved": channel.on_resolved,
        "enabled": channel.enabled,
        "site_id": str(sid) if (sid := scoping.row_site_id(channel)) else None,
        "last_ok": channel.last_ok,
        "last_status": channel.last_status,
        "last_status_at": channel.last_status_at.isoformat() if channel.last_status_at else None,
        "created_at": channel.created_at.isoformat(),
    }


def _kind_or_error(raw):
    kind = channels.get(str(raw or ""))
    if kind is None:
        return None, Response({"kind": [f"unknown channel type {raw!r}"]}, status=400)
    if kind.feature and not licensing.has_feature(kind.feature):
        return None, Response(upgrade_body(kind.feature), status=402)
    return kind, None


def _name_or_error(raw, exclude=None):
    name = str(raw or "").strip()
    if not name:
        return None, Response({"name": ["A channel needs a name."]}, status=400)
    if len(name) > 255:
        return None, Response({"name": ["At most 255 characters."]}, status=400)
    clash = NotificationChannel.objects.filter(name__iexact=name)
    if exclude is not None:
        clash = clash.exclude(pk=exclude.pk)
    # Deliberately unscoped (see _NAME_UNIQUE): a name taken in another site
    # is still taken.
    if clash.exists():
        return None, Response({"name": [f"A channel named {name!r} already exists."]}, status=400)
    return name, None


def _flags(channel, data):
    for flag in ("on_firing", "on_resolved", "enabled"):
        if flag in data:
            setattr(channel, flag, bool(data[flag]))


@extend_schema(responses=AlertChannelTypeSerializer(many=True))
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def channel_types(request):
    """Every channel type, with the fields its form asks for."""
    return Response([t.as_dict() for t in channels.all_types()])


@extend_schema(methods=["GET"], responses=AlertChannelSerializer(many=True))
@extend_schema(methods=["POST"], request=AlertChannelWriteSerializer,
               responses={201: AlertChannelSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def channel_index(request):
    if request.method == "GET":
        return Response([_row(c) for c in _visible(request.user).order_by("name")])

    data = request.data
    kind, err = _kind_or_error(data.get("kind"))
    if err:
        return err
    name, err = _name_or_error(data.get("name"))
    if err:
        return err
    site_id, denied = scoping.site_for_write(request.user, data.get("site_id"))
    if denied:
        return denied
    config, errors = channels.clean_config(kind, data.get("config"))
    if errors:
        return Response({"config": errors}, status=400)
    channel = NotificationChannel(name=name, kind=kind.kind, config=config)
    _flags(channel, data)
    with transaction.atomic():
        channel.save()
        scoping.place(channel, site_id)
    return Response(_row(channel), status=status.HTTP_201_CREATED)


@extend_schema(methods=["GET"], responses=AlertChannelSerializer)
@extend_schema(methods=["PATCH"], request=AlertChannelWriteSerializer,
               responses=AlertChannelSerializer)
@extend_schema(methods=["DELETE"], responses={204: None})
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def channel_detail(request, channel_id):
    channel = get_object_or_404(_visible(request.user), pk=channel_id)
    if request.method == "GET":
        return Response(_row(channel))
    if denied := scoping.write_denied(request.user, channel):
        return denied
    if request.method == "DELETE":
        channel.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data
    new_site = None
    if "site_id" in data:
        new_site, denied = scoping.site_for_write(request.user, data["site_id"])
        if denied:
            return denied
    if "name" in data:
        channel.name, err = _name_or_error(data["name"], exclude=channel)
        if err:
            return err
    if "kind" in data and data["kind"] != channel.kind:
        return Response({"kind": ["A channel's type cannot change: make a new channel."]},
                        status=400)
    if "config" in data:
        kind, err = _kind_or_error(channel.kind)
        if err:
            return err
        config, errors = channels.clean_config(kind, data["config"], existing=channel.config)
        if errors:
            return Response({"config": errors}, status=400)
        channel.config = config
    _flags(channel, data)
    with transaction.atomic():
        channel.save()
        if "site_id" in data:
            scoping.place(channel, new_site)
    return Response(_row(channel))


@extend_schema(request=None, responses=AlertChannelTestSerializer)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
@throttle_classes([_TestSendThrottle])
def channel_test(request, channel_id):
    """Send a test message now. The answer says whether it was delivered and,
    if not, why, in Vigil's words: never the remote server's response body."""
    channel = get_object_or_404(_visible(request.user), pk=channel_id)
    if denied := scoping.write_denied(request.user, channel):
        return denied
    payload = {
        "event": "test",
        "alert_id": None,
        "host": "Vigil",
        "host_id": None,
        "severity": "info",
        "message": f"Test message from Vigil for the channel {channel.name!r}. "
                   f"If you can read this, it works.",
        "metric_value": None,
        "rule": None,
        "fired_at": now().isoformat(),
        "resolved_at": None,
    }
    from .notifications import message_for

    ok = deliver(channel, message_for(payload))
    channel.refresh_from_db(fields=["last_ok", "last_status", "last_status_at"])
    return Response({"ok": ok, "detail": channel.last_status, "channel": _row(channel)})
