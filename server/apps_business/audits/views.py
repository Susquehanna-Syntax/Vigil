"""Audit log API — the viewer is the Business feature (402 unlicensed);
recording happens regardless. CSV export included: 'can I hand this to an
auditor' is the whole purchase motivation."""

import csv

from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil.licensing import require_feature

from .models import AuditEvent

FIELDS = ("id", "created_at", "username", "action", "target", "auth_method",
          "ip", "detail")


def _filtered(request):
    qs = AuditEvent.objects.all()
    if action := request.GET.get("action", ""):
        qs = qs.filter(action=action)
    if username := request.GET.get("user", ""):
        qs = qs.filter(username=username)
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin, require_feature("audit_log")])
def audit_list(request):
    paginator = PageNumberPagination()
    paginator.page_size = 50
    page = paginator.paginate_queryset(_filtered(request), request)
    return paginator.get_paginated_response([
        {
            "id": str(e.id),
            "created_at": e.created_at.isoformat(),
            "username": e.username,
            "action": e.action,
            "target": e.target,
            "auth_method": e.auth_method,
            "ip": e.ip,
            "detail": e.detail,
        }
        for e in page
    ])


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin, require_feature("audit_log")])
def audit_export(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="vigil-audit-log.csv"'
    writer = csv.writer(response)
    writer.writerow(FIELDS)
    for e in _filtered(request).iterator():
        writer.writerow([e.id, e.created_at.isoformat(), e.username, e.action,
                         e.target, e.auth_method, e.ip or "", e.detail])
    return response
