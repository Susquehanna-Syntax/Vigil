"""Maintenance windows API (sprint M7).

Admins schedule a window here: a one-off stretch, or a weekly recurrence in a
time zone. While a window is active the hosts it covers raise no metric alerts
and send no notifications (apps/alerts/maintenance.py). Site rules are the
status pages' ones — a plain ``site_id`` column, null meaning global.
"""
import uuid
from datetime import time as _time_type
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.models import Host
from vigil import scoping

from .api_schema import AlertErrorSerializer
from .channel_schema import (
    MaintenanceWindowSerializer,
    MaintenanceWindowWriteSerializer,
)
from .maintenance import MAX_WEEKLY_MINUTES, is_active
from .models import MaintenanceWindow

_MAX_NAME = 200
_MAX_TAG_LEN = 100


def _visible(user):
    return scoping.filter_by_site_field(MaintenanceWindow.objects.all(), user)


def _row(window: MaintenanceWindow) -> dict:
    return {
        "id": str(window.id),
        "name": window.name,
        "site_id": str(window.site_id) if window.site_id else None,
        "kind": window.kind,
        "starts_at": window.starts_at.isoformat() if window.starts_at else None,
        "ends_at": window.ends_at.isoformat() if window.ends_at else None,
        "weekdays": list(window.weekdays or []),
        "start_time": window.start_time.strftime("%H:%M") if window.start_time else None,
        "duration_minutes": window.duration_minutes,
        "timezone": window.timezone,
        "host_ids": [str(h) for h in window.hosts.values_list("pk", flat=True)],
        "tags": list(window.tags or []),
        "enabled": window.enabled,
        "active": is_active(window),
    }


def _err(sentence):
    return Response({"error": sentence}, status=400)


def _clean_name(raw):
    name = str(raw if raw is not None else "").strip()
    if not name:
        return None, _err("A maintenance window needs a name.")
    if len(name) > _MAX_NAME:
        return None, _err(f"Name must be at most {_MAX_NAME} characters.")
    return name, None


def _aware_or_error(raw):
    """An aware datetime parsed from *raw* (an ISO-8601 string), or an error."""
    ts = parse_datetime(raw.strip()) if isinstance(raw, str) else None
    if ts is None or ts.tzinfo is None or ts.utcoffset() is None:
        return None, _err("Expected an ISO-8601 datetime with a timezone offset.")
    return ts, None


def _clean_once(starts_at, ends_at):
    start, err = _aware_or_error(starts_at)
    if err:
        return None, None, err
    end, err = _aware_or_error(ends_at)
    if err:
        return None, None, err
    if end <= start:
        return None, None, _err("ends_at must be after starts_at.")
    return start, end, None


def _weekdays_error(weekdays):
    if not isinstance(weekdays, list) or not weekdays:
        return _err("A weekly window needs at least one weekday (Monday is 0, Sunday is 6).")
    days = []
    for day in weekdays:
        # bool is an int subclass: True as a weekday is a typo, not Monday.
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            return _err("weekdays must be a list of distinct whole numbers from 0 to 6.")
        days.append(day)
    if len(set(days)) != len(days):
        return _err("weekdays must not repeat a day.")
    return None


def _start_time_error(start_time):
    if not isinstance(start_time, str):
        return _err("start_time must be HH:MM.")
    parts = start_time.strip().split(":")
    if len(parts) != 2:
        return _err("start_time must be HH:MM.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return _err("start_time must be HH:MM.")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return _err("start_time must be HH:MM.")
    return None


def _duration_error(duration_minutes):
    if (isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int)
            or not 1 <= duration_minutes <= MAX_WEEKLY_MINUTES):
        return _err(f"duration_minutes must be between 1 and {MAX_WEEKLY_MINUTES}.")
    return None


def _timezone_error(tz_name):
    name = str(tz_name or "").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _err(f"Unknown timezone: {name}")
    return None


def _clean_tags(raw):
    if not isinstance(raw, list):
        return None, _err("tags must be a list of tag names.")
    tags = []
    for tag in raw:
        if not isinstance(tag, str) or len(tag) > _MAX_TAG_LEN:
            return None, _err(f"Each tag must be at most {_MAX_TAG_LEN} characters.")
        tags.append(tag)
    return tags, None


def _clean_host_ids(raw, user, site_id):
    """The host rows the window may name, or a 400.

    A host in another site and a host that does not exist answer with the same
    words, so a guess at an id reveals nothing about another site."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, _err("host_ids must be a list of host ids.")
    ids = set()
    for raw_id in raw:
        try:
            ids.add(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            return None, _err(f"Unknown host: {raw_id}")
    if not ids:
        return [], None
    found = list(scoping.filter_by_site(Host.objects.filter(pk__in=ids), user))
    if missing := ids - {h.pk for h in found}:
        return None, _err(f"Unknown host: {min(missing)}")
    if site_id is not None:
        scoping.prime_site_ids(found)
        if foreign := [h.pk for h in found if scoping.site_id_of(h) != site_id]:
            return None, _err(f"Unknown host: {min(foreign)}")
    return found, None


def _clean(data, user, site_id, window_kind, partial=False):
    """Validated field values for POST (all required) and PATCH (partial: a
    field left out keeps what is stored, ``kind`` included).

    *window_kind* is the stored kind on a PATCH, None on create.

    Returns ``(values, hosts, error_response)``; on any failure the response is
    set and the caller returns it.
    """
    values = {}
    hosts = None

    if "name" in data or not partial:
        name, err = _clean_name(data.get("name"))
        if err:
            return values, hosts, err
        values["name"] = name

    if "kind" in data or not partial:
        kind = data.get("kind")
        if kind not in (MaintenanceWindow.Kind.ONCE, MaintenanceWindow.Kind.WEEKLY):
            return values, hosts, _err("kind is either 'once' or 'weekly'.")
        values["kind"] = kind

    # The schedule fields belong to the window's kind: a one-off answers for
    # its two ends, a weekly recurrence for its weekdays, time and duration.
    if "starts_at" in data or "ends_at" in data or not partial:
        if (values.get("kind") or window_kind) == MaintenanceWindow.Kind.ONCE:
            start, end, err = _clean_once(data.get("starts_at"), data.get("ends_at"))
            if err:
                return values, hosts, err
            values["starts_at"], values["ends_at"] = start, end
        else:
            for err in (_weekdays_error(data.get("weekdays")),
                        _start_time_error(data.get("start_time")),
                        _duration_error(data.get("duration_minutes", 60)),
                        _timezone_error(data.get("timezone", "UTC"))):
                if err is not None:
                    return values, hosts, err
            hour, minute = (int(p) for p in data["start_time"].strip().split(":"))
            values["weekdays"] = list(data["weekdays"])
            values["start_time"] = _time_type(hour, minute)
            values["duration_minutes"] = data["duration_minutes"]
            values["timezone"] = str(data.get("timezone", "UTC")).strip()

    if "host_ids" in data:
        hosts, err = _clean_host_ids(data["host_ids"], user, site_id)
        if err:
            return values, hosts, err

    if "tags" in data:
        tags, err = _clean_tags(data["tags"])
        if err:
            return values, hosts, err
        values["tags"] = tags

    if "enabled" in data:
        values["enabled"] = bool(data["enabled"])

    return values, hosts, None


@extend_schema(methods=["GET"], responses=MaintenanceWindowSerializer(many=True))
@extend_schema(methods=["POST"], request=MaintenanceWindowWriteSerializer,
               responses={201: MaintenanceWindowSerializer, 400: AlertErrorSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def window_index(request):
    if request.method == "GET":
        return Response([_row(w) for w in _visible(request.user).order_by("-created_at")])

    data = request.data
    site_id, denied = scoping.site_for_write(request.user, data.get("site_id"))
    if denied:
        return denied
    values, hosts, err = _clean(data, request.user, site_id, None)
    if err is not None:
        return err
    window = MaintenanceWindow(site_id=site_id, **values)
    window.save()
    if hosts:
        window.hosts.set(hosts)
    return Response(_row(window), status=status.HTTP_201_CREATED)


@extend_schema(methods=["GET"], responses=MaintenanceWindowSerializer)
@extend_schema(methods=["PATCH"], request=MaintenanceWindowWriteSerializer,
               responses={200: MaintenanceWindowSerializer, 400: AlertErrorSerializer})
@extend_schema(methods=["DELETE"], responses={204: None})
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def window_detail(request, window_id):
    window = get_object_or_404(_visible(request.user), pk=window_id)
    if request.method == "GET":
        return Response(_row(window))
    if denied := scoping.write_denied(request.user, window):
        return denied
    if request.method == "DELETE":
        window.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data
    site_id = window.site_id
    if "site_id" in data:
        site_id, denied = scoping.site_for_write(request.user, data["site_id"])
        if denied:
            return denied

    values, hosts, err = _clean(data, request.user, site_id, window.kind, partial=True)
    if err is not None:
        return err

    for field, value in values.items():
        setattr(window, field, value)
    window.site_id = site_id
    window.save()
    if hosts is not None:
        window.hosts.set(hosts)
    return Response(_row(window))
