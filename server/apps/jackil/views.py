"""What the Settings pane shows once the integration is working.

A form that saves is not proof that anything happened. The last few tickets
Vigil actually opened are, which is why this exists at all.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin

from .models import JackilTicket


@api_view(["GET"])
@permission_classes([IsAdmin])
def recent_tickets(request):
    rows = (JackilTicket.objects
            .select_related("alert", "alert__host")
            .order_by("-created_at")[:5])
    return Response({
        "count": JackilTicket.objects.count(),
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
