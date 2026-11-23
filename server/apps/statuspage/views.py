from datetime import timedelta

from django.shortcuts import get_object_or_404, render
from django.utils.timezone import now
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.alerts.maintenance import covers, hosts_in_maintenance, occurrence
from apps.alerts.models import MaintenanceWindow
from apps.hosts.models import Host
from vigil import scoping
from vigil.licensing import has_feature, upgrade_body

from .api_schema import (
    StatusPageDetailSerializer,
    StatusPageErrorSerializer,
    StatusPageHostChoiceSerializer,
    StatusPageSerializer,
    StatusPageUpgradeSerializer,
    StatusPageUptimeDaySerializer,
    StatusPageWriteSerializer,
)
from .models import StatusPage

BRANDING_FIELDS = ("site_id", "logo_url", "hide_badge")


UPTIME_WINDOW_DAYS = 90


def _uptime_history(host_objs):
    """Daily uptime bars + overall % for each host over the render window.

    Returns ``{host_id: {"bars": [...], "pct": float|None}}`` where each bar is
    ``{"pct": float|None, "state": "up|degraded|down|nodata", "label": str}``.
    Availability comes from HostUptimeSample; a day with no samples is neutral.
    """
    from datetime import timedelta

    from django.db.models import Count, Q
    from django.db.models.functions import TruncDate
    from django.utils.timezone import localdate

    from .models import HostUptimeSample

    today = localdate()
    start = today - timedelta(days=UPTIME_WINDOW_DAYS - 1)
    days = [start + timedelta(days=i) for i in range(UPTIME_WINDOW_DAYS)]

    rows = (HostUptimeSample.objects
            .filter(host__in=host_objs, time__date__gte=start)
            .annotate(day=TruncDate("time"))
            .values("host_id", "day")
            .annotate(total=Count("id"), up=Count("id", filter=Q(up=True))))

    by_host: dict = {}
    for r in rows:
        by_host.setdefault(str(r["host_id"]), {})[r["day"]] = (r["up"], r["total"])

    history = {}
    for h in host_objs:
        per_day = by_host.get(str(h.id), {})
        bars, up_total, samp_total = [], 0, 0
        for day in days:
            up, total = per_day.get(day, (0, 0))
            if total:
                pct = up / total
                state = ("up" if pct >= 0.995 else
                         "degraded" if pct >= 0.90 else "down")
                label = f"{day:%b %-d}: {pct * 100:.1f}% up"
                up_total += up
                samp_total += total
            else:
                pct, state = None, "nodata"
                label = f"{day:%b %-d}: no data"
            bars.append({"pct": pct, "state": state, "label": label})
        history[str(h.id)] = {
            "bars": bars,
            "pct": (up_total / samp_total) if samp_total else None,
        }
    return history


def _public_key(page, host) -> str:
    import hashlib
    import hmac
    return hmac.new(page.token.encode(), str(host.id).encode(), hashlib.sha256).hexdigest()[:16]


NOTICE_HORIZON = timedelta(days=7)
MAX_NOTICES = 5


def _card_state(in_maintenance: bool, up: bool) -> str:
    """The state key the public page renders one host as. The template's CSS
    and its live-refresh script both switch on these exact strings."""
    if in_maintenance:
        return "maintenance"
    return "up" if up else "down"


def _page_view(page):
    """Everything the page renders: host rows with their state, the headline
    flags, and the maintenance notices. Shared by the HTML page and its polling
    JSON endpoint."""
    labels = page.host_labels or {}
    host_objs = list(page.hosts())
    scoping.prime_site_ids(host_objs)
    in_maint = hosts_in_maintenance(host_objs)
    history = _uptime_history(host_objs)
    hosts = []
    for h in host_objs:
        hist = history.get(str(h.id), {})
        pct = hist.get("pct")
        up = h.status == Host.Status.ONLINE
        state = _card_state(h.pk in in_maint, up)
        hosts.append({
            # Opaque and per page: the live refresh only needs a stable key to
            # find each card, and the public page should not publish the
            # host's internal id.
            "id": _public_key(page, h),
            "hostname": labels.get(str(h.id)) or h.hostname,
            "up": up,
            "state": state,
            "bars": hist.get("bars", []),
            "uptime_pct": f"{pct * 100:.2f}" if pct is not None else None,
        })
    return {
        "hosts": hosts,
        "all_up": not any(h["state"] == "down" for h in hosts),
        "up_count": sum(1 for h in host_objs if h.status == Host.Status.ONLINE),
        "maintenance_count": len(in_maint),
        "notices": _notices(page, host_objs, labels),
    }


def _notices(page, host_objs, labels):
    """The maintenance windows covering this page's hosts that are running now
    or start within NOTICE_HORIZON. The window's own name is internal and never
    reaches the public page."""
    moment = now()
    notices = []
    for window in MaintenanceWindow.objects.filter(
            enabled=True).prefetch_related("hosts"):
        occ = occurrence(window, moment, NOTICE_HORIZON)
        if occ is None:
            continue
        start, end = occ
        covered = sorted({labels.get(str(h.id)) or h.hostname
                          for h in host_objs if covers(window, h)})
        if not covered:
            continue
        notices.append({"start": start, "end": end,
                        "hosts": covered})
    notices.sort(key=lambda n: (not (n["start"] <= moment), n["start"]))
    out = []
    for n in notices[:MAX_NOTICES]:
        out.append({
            "starts_at": n["start"].isoformat(),
            "ends_at": n["end"].isoformat(),
            "active": n["start"] <= moment,
            "hosts": n["hosts"],
            "start": n["start"],
            "end": n["end"],
        })
    return out


@scoping.system_view(reason="public status page: the page token is the access control")
def public_status(request, token):
    """The public page. No auth — the token IS the access control. Branding
    fields only render when the license carries status_branding, so a lapsed
    Business page cleanly falls back to the free look instead of breaking."""
    page = get_object_or_404(StatusPage, token=token, enabled=True)
    branded = has_feature("status_branding")
    view = _page_view(page)
    return render(request, "status_public.html", {
        "page": page,
        "hosts": view["hosts"],
        "all_up": view["all_up"],
        "up_count": view["up_count"],
        "maintenance_count": view["maintenance_count"],
        "notices": view["notices"],
        "window_days": UPTIME_WINDOW_DAYS,
        "branded": branded,
        "show_badge": not (branded and page.hide_badge),
        "logo_url": page.logo_url if branded else "",
    })


@scoping.system_view(reason="public status page: the page token is the access control")
def public_status_data(request, token):
    """JSON snapshot of a page's current status — polled by the live page so it
    refreshes in place without a full reload (which would replay animations).
    Same token-only access model as the HTML page."""
    from django.http import JsonResponse

    page = get_object_or_404(StatusPage, token=token, enabled=True)
    view = _page_view(page)
    return JsonResponse({
        "all_up": view["all_up"],
        "up_count": view["up_count"],
        "total": len(view["hosts"]),
        "maintenance_count": view["maintenance_count"],
        "hosts": view["hosts"],
        "notices": [{k: n[k] for k in ("starts_at", "ends_at", "active", "hosts")}
                    for n in view["notices"]],
    })


def _row(p: StatusPage) -> dict:
    return {
        "id": str(p.id), "token": p.token, "title": p.title,
        "enabled": p.enabled, "host_ids": [str(h) for h in (p.host_ids or [])],
        "host_labels": p.host_labels or {},
        "site_id": str(p.site_id) if p.site_id else None,
        "logo_url": p.logo_url, "hide_badge": p.hide_badge,
        "url": f"/status/{p.token}/",
        "is_primary": p.is_primary,
    }


@extend_schema(responses=StatusPageHostChoiceSerializer(many=True))
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def selectable_hosts(request):
    """Every non-pending host the caller may see, for the machine picker.

    Scoped like every other host list: an admin confined to one site picking
    machines for a status page must not be offered another site's hostnames.
    """
    hosts = scoping.filter_by_site(
        Host.objects.exclude(status=Host.Status.PENDING).exclude(
            status=Host.Status.REJECTED),
        request.user,
    ).order_by("hostname")
    return Response([{"id": str(h.id), "hostname": h.hostname,
                      "up": h.status == Host.Status.ONLINE} for h in hosts])


@extend_schema(methods=["GET"], responses=StatusPageSerializer(many=True))
@extend_schema(methods=["POST"], request=StatusPageWriteSerializer,
               responses={201: StatusPageSerializer,
                          402: StatusPageUpgradeSerializer,
                          403: StatusPageDetailSerializer})
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def page_index(request):
    if request.method == "GET":
        return Response([_row(p) for p in scoping.filter_by_site_field(
            StatusPage.objects.all(), request.user).order_by("created_at")])
    # The FIRST page is free. Additional pages (per-site/client) are Business.
    # Counted across every site: the free-page limit is the install's.
    every = StatusPage.objects.unscoped(reason="free status page limit is per install")
    if every.exists() and not has_feature("status_branding"):
        return Response(upgrade_body("status_branding"), status=402)
    page = StatusPage()
    err = _apply(page, request)
    return err if err is not None else Response(_row(page), status=http.HTTP_201_CREATED)


@extend_schema(methods=["PATCH"], request=StatusPageWriteSerializer,
               responses={200: StatusPageSerializer,
                          400: StatusPageDetailSerializer,
                          402: StatusPageUpgradeSerializer,
                          403: StatusPageDetailSerializer})
@extend_schema(methods=["DELETE"], request=None,
               responses={(204, "application/json"): None,
                          400: StatusPageErrorSerializer,
                          403: StatusPageDetailSerializer})
@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def page_detail(request, page_id):
    page = get_object_or_404(
        scoping.filter_by_site_field(StatusPage.objects.all(), request.user),
        pk=page_id)
    if denied := scoping.write_denied(request.user, page):
        return denied
    if request.method == "DELETE":
        page.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)
    err = _apply(page, request)
    return err if err is not None else Response(_row(page))


def _host_ids_error(user, raw, site_id):
    """A 400 unless every id is a host *user* can see and, on a site's page,
    a host in that site. The page is public: listing a host publishes it."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, list):
        return Response({"detail": "host_ids must be a list"}, status=400)
    import uuid as _uuid
    try:
        wanted = {_uuid.UUID(str(h)) for h in raw}
    except ValueError:
        return Response({"detail": "host_ids must be host ids"}, status=400)
    qs = scoping.filter_by_site(Host.objects.filter(pk__in=wanted), user)
    if site_id:
        qs = qs.filter(site_assignment__site_id=site_id)
    if set(qs.values_list("pk", flat=True)) != wanted:
        return Response({"detail": "host_ids may only name hosts you can see"
                                   + (" in this page's site" if site_id else "")}, status=400)
    return None


def _apply(page, request):
    """Apply editable fields; branding fields need the license. Returns an
    error Response or None."""
    data = dict(request.data)
    if any(f in data for f in BRANDING_FIELDS) and not has_feature("status_branding"):
        return Response(upgrade_body("status_branding"), status=402)
    # Sites (S1 04e): a new page goes in a site the caller may write in (only
    # an administrator of the whole install makes a global one), and moving a
    # page is a create in the new site.
    if page._state.adding or "site_id" in data:
        sid, denied = scoping.site_for_write(request.user, data.get("site_id"))
        if denied:
            return denied
        data["site_id"] = sid
    if "host_ids" in data:
        if err := _host_ids_error(request.user, data["host_ids"],
                                  data.get("site_id", page.site_id)):
            return err
    for field in ("title", "enabled", "host_ids", "host_labels", *BRANDING_FIELDS):
        if field in data:
            setattr(page, field, data[field] if data[field] != "" else
                    ("" if field == "logo_url" else data[field]))
    if data.get("rotate_token"):
        from .models import _token
        page.token = _token()
    page.save()
    return None


@extend_schema(parameters=[
    OpenApiParameter("days", int,
                     description="How many days of history to return. Default 30, clamped between 1 and 90."),
], responses=StatusPageUptimeDaySerializer(many=True))
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_uptime(request, host_id):
    """Daily up/down ratio for one host.

    The samples already back the public status page's bars; this exposes them
    to the dashboard's uptime widget so the two read the same history rather
    than computing availability twice from different places.
    """
    from datetime import timedelta

    from django.db.models import Avg, IntegerField
    from django.db.models.functions import Cast, TruncDate
    from django.utils.timezone import now

    from .models import HostUptimeSample

    try:
        days = max(1, min(90, int(request.query_params.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    # Scoped, not just authenticated. The host *list* is narrowed by site, so a
    # per-host read that is not would let an operator confined to one site pull
    # another site's history by id — which is the whole point of Sites.
    _, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

    rows = (HostUptimeSample.objects
            .filter(host_id=host_id, time__gte=now() - timedelta(days=days))
            .annotate(day=TruncDate("time"))
            .values("day")
            # Cast to int before averaging: PostgreSQL has no avg(boolean), so
            # a bare Avg("up") is a 500 there even though SQLite accepts it.
            # Integer, not float — Postgres will not cast boolean to float8.
            .annotate(ratio=Avg(Cast("up", IntegerField())))
            .order_by("day"))
    return Response([
        {"day": r["day"].isoformat(), "uptime": round(float(r["ratio"] or 0) * 100, 1)}
        for r in rows
    ])
