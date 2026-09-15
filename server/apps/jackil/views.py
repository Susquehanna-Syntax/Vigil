"""What the Settings pane shows once the integration is working.

A form that saves is not proof that anything happened. The last few tickets
Vigil actually opened are, which is why this exists at all.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil import scoping

from .models import JackilTicket


@api_view(["GET"])
@permission_classes([IsAdmin])
def recent_tickets(request):
    # Scoped by site, like the alert list these rows came from. IsAdmin asks
    # the unscoped question — role_of(user) answers with a scoped user's
    # strongest role anywhere — so an admin of one site passes it. Without
    # this they would then read every site's hostnames and alert text, which
    # is precisely what a site boundary is for. A ticket hangs off an alert
    # which hangs off a host, hence the two-step path; membership semantics
    # (cascade_global=False) match Alert, because a ticket is about one host
    # rather than a rule that applies everywhere.
    visible = scoping.filter_by_site(
        JackilTicket.objects.all(), request.user, path="alert__host__")
    rows = (visible
            .select_related("alert", "alert__host")
            .order_by("-created_at")[:5])
    return Response({
        "count": visible.count(),
        "tickets": [{
            "ticket_id": row.ticket_id,
            "url": row.url,
            "host": row.alert.host.hostname,
            "severity": row.alert.severity,
            "message": row.alert.message,
            "created_at": row.created_at,
            "cleared_at": row.cleared_at,
        } for row in rows],
    })
