"""The patch compliance report — the Business half (licence: compliance_reports).

The numbers are Free (apps/policies/compliance.py). What a third party
reads is this: pass or fail per site against the patch SLA, every host's
overdue count, a CSV export, and a printable page in the instance's own
branding with "Licensed to <org>" in the footer.
"""

import csv
import io

from django.http import HttpResponse
from django.template.loader import render_to_string
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.policies.compliance import compliance
from vigil import licensing
from vigil.licensing import require_feature

FEATURE = "compliance_reports"


def _licensee() -> str:
    state = licensing.current_state()
    return state.claims.org if state.claims else ""


@api_view(["GET"])
@permission_classes([IsAuthenticated, require_feature(FEATURE)])
def report(request):
    return Response({**compliance(request.user), "licensed_to": _licensee()})


@api_view(["GET"])
@permission_classes([IsAuthenticated, require_feature(FEATURE)])
def report_csv(request):
    data = compliance(request.user)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["host", "pending_updates", "overdue_updates", "oldest_overdue_days",
                     "patched_within_sla"])
    for row in data["hosts"]:
        writer.writerow([row["hostname"], row["pending"], row["overdue"],
                         row["oldest_overdue_days"], "yes" if row["overdue"] == 0 else "no"])
    response = HttpResponse(buf.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="patch-compliance.csv"'
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated, require_feature(FEATURE)])
def report_html(request):
    """A printable page: the browser's Print → PDF is the PDF export."""
    from apps_business.branding.models import BrandingConfig

    brand = BrandingConfig.load()
    html = render_to_string("compliance_report.html", {
        "data": compliance(request.user),
        "brand": brand,
        "product": brand.product_name or "Vigil",
        "accent": brand.accent or "#7eddb5",
        "licensed_to": _licensee(),
    })
    return HttpResponse(html)
