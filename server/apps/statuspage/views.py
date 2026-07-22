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


def public_status(request, token):
    """The public page. No auth — the token IS the access control. Branding
    fields only render when the license carries status_branding, so a lapsed
    Business page cleanly falls back to the free look instead of breaking."""
    page = get_object_or_404(StatusPage, token=token, enabled=True)
    branded = has_feature("status_branding")
    labels = page.host_labels or {}
    hosts = [
        {"hostname": labels.get(str(h.id)) or h.hostname,
         "up": h.status == Host.Status.ONLINE}
        for h in page.hosts()
    ]
    return render(request, "status_public.html", {
        "page": page,
        "hosts": hosts,
        "all_up": all(h["up"] for h in hosts) if hosts else True,
        "up_count": sum(1 for h in hosts if h["up"]),
        "branded": branded,
        "show_badge": not (branded and page.hide_badge),
        "logo_url": page.logo_url if branded else "",
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
