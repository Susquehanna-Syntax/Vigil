from django.shortcuts import get_object_or_404, render
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.models import Host
from vigil.licensing import has_feature, upgrade_body

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


def _page_hosts(page):
    """The host rows the page renders: current up/down, uptime bars, and the
    overall percent. Shared by the HTML page and its polling JSON endpoint."""
    labels = page.host_labels or {}
    host_objs = list(page.hosts())
    history = _uptime_history(host_objs)
    hosts = []
    for h in host_objs:
        hist = history.get(str(h.id), {})
        pct = hist.get("pct")
        hosts.append({
            "id": str(h.id),
            "hostname": labels.get(str(h.id)) or h.hostname,
            "up": h.status == Host.Status.ONLINE,
            "bars": hist.get("bars", []),
            "uptime_pct": f"{pct * 100:.2f}" if pct is not None else None,
        })
    return hosts


def public_status(request, token):
    """The public page. No auth — the token IS the access control. Branding
    fields only render when the license carries status_branding, so a lapsed
    Business page cleanly falls back to the free look instead of breaking."""
    page = get_object_or_404(StatusPage, token=token, enabled=True)
    branded = has_feature("status_branding")
    hosts = _page_hosts(page)
    return render(request, "status_public.html", {
        "page": page,
        "hosts": hosts,
        "all_up": all(h["up"] for h in hosts) if hosts else True,
        "up_count": sum(1 for h in hosts if h["up"]),
        "window_days": UPTIME_WINDOW_DAYS,
        "branded": branded,
        "show_badge": not (branded and page.hide_badge),
        "logo_url": page.logo_url if branded else "",
    })


def public_status_data(request, token):
    """JSON snapshot of a page's current status — polled by the live page so it
    refreshes in place without a full reload (which would replay animations).
    Same token-only access model as the HTML page."""
    from django.http import JsonResponse

    page = get_object_or_404(StatusPage, token=token, enabled=True)
    hosts = _page_hosts(page)
    return JsonResponse({
        "all_up": all(h["up"] for h in hosts) if hosts else True,
        "up_count": sum(1 for h in hosts if h["up"]),
        "total": len(hosts),
        "hosts": hosts,
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


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def selectable_hosts(request):
    """Every non-pending host, for the status-page machine picker."""
    hosts = Host.objects.exclude(status=Host.Status.PENDING).exclude(
        status=Host.Status.REJECTED).order_by("hostname")
    return Response([{"id": str(h.id), "hostname": h.hostname,
                      "up": h.status == Host.Status.ONLINE} for h in hosts])


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def page_index(request):
    if request.method == "GET":
        return Response([_row(p) for p in StatusPage.objects.order_by("created_at")])
    # The FIRST page is free. Additional pages (per-site/client) are Business.
    if StatusPage.objects.exists() and not has_feature("status_branding"):
        return Response(upgrade_body("status_branding"), status=402)
    page = StatusPage.objects.create()
    return Response(_apply(page, request) or _row(page),
                    status=http.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def page_detail(request, page_id):
    page = get_object_or_404(StatusPage, pk=page_id)
    if request.method == "DELETE":
        page.delete()
        return Response(status=http.HTTP_204_NO_CONTENT)
    err = _apply(page, request)
    return err if err is not None else Response(_row(page))


def _apply(page, request):
    """Apply editable fields; branding fields need the license. Returns an
    error Response or None."""
    data = request.data
    if any(f in data for f in BRANDING_FIELDS) and not has_feature("status_branding"):
        return Response(upgrade_body("status_branding"), status=402)
    for field in ("title", "enabled", "host_ids", "host_labels", *BRANDING_FIELDS):
        if field in data:
            setattr(page, field, data[field] if data[field] != "" else
                    ("" if field == "logo_url" else data[field]))
    if data.get("rotate_token"):
        from .models import _token
        page.token = _token()
    page.save()
    return None
