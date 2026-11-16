"""Is a host inside a maintenance window right now?

Two sources, either one is enough: Host.maintenance_until (set by a rebuild,
docs/reprovisioning.md section 6) and MaintenanceWindow rows.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.timezone import now

from vigil import scoping

from .models import MaintenanceWindow

#: A weekly window may last at most a day: is_active only looks back one day.
MAX_WEEKLY_MINUTES = 24 * 60


def is_active(window, at=None) -> bool:
    at = at or now()
    if not window.enabled:
        return False
    if window.kind == MaintenanceWindow.Kind.ONCE:
        return bool(window.starts_at and window.ends_at and window.starts_at <= at < window.ends_at)
    if window.start_time is None or not window.weekdays:
        return False
    try:
        tz = ZoneInfo(window.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = at.astimezone(tz)
    length = timedelta(minutes=min(window.duration_minutes, MAX_WEEKLY_MINUTES))
    for day in (local.date(), local.date() - timedelta(days=1)):
        if day.weekday() not in window.weekdays:
            continue
        start = datetime.combine(day, window.start_time, tzinfo=tz)
        if start <= local < start + length:
            return True
    return False


def occurrence(window, after, horizon):
    """``(start, end)`` of *window*'s occurrence that is running at *after*, or
    else the first one starting before ``after + horizon``; None when there is
    none. Both are aware datetimes. A disabled window has none."""
    if not window.enabled:
        return None
    if window.kind == MaintenanceWindow.Kind.ONCE:
        if not (window.starts_at and window.ends_at):
            return None
        if window.ends_at > after and window.starts_at < after + horizon:
            return window.starts_at, window.ends_at
        return None
    if window.start_time is None or not window.weekdays:
        return None
    try:
        tz = ZoneInfo(window.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local_after = after.astimezone(tz)
    length = timedelta(minutes=min(window.duration_minutes, MAX_WEEKLY_MINUTES))
    first = local_after.date() - timedelta(days=1)
    for offset in range(horizon.days + 2):
        day = first + timedelta(days=offset)
        if day.weekday() not in window.weekdays:
            continue
        start = datetime.combine(day, window.start_time, tzinfo=tz)
        end = start + length
        if end > after and start < after + horizon:
            return start, end
    return None


def covers(window, host) -> bool:
    """Does *window* apply to *host*, ignoring time?"""
    sid = window.site_id
    if sid is not None and scoping.site_id_of(host) != sid:
        return False
    host_ids = {h.pk for h in window.hosts.all()}
    if not host_ids and not window.tags:
        return True
    if host.pk in host_ids:
        return True
    names = {str(t).lower() for t in (host.tags or [])}
    return any(str(t).lower() in names for t in window.tags)


def active_windows(at=None) -> list:
    at = at or now()
    rows = MaintenanceWindow.objects.filter(enabled=True).prefetch_related("hosts")
    return [w for w in rows if is_active(w, at)]


def in_maintenance(host, at=None, windows=None) -> bool:
    at = at or now()
    if host.maintenance_until is not None and host.maintenance_until > at:
        return True
    if windows is None:
        windows = active_windows(at)
    return any(covers(w, host) for w in windows)


def hosts_in_maintenance(hosts, at=None) -> set:
    """The ids of *hosts* inside a window, with one query for the windows."""
    at = at or now()
    hosts = list(hosts)
    windows = active_windows(at)
    if windows:
        scoping.prime_site_ids(hosts)
    return {h.pk for h in hosts if in_maintenance(h, at, windows)}
