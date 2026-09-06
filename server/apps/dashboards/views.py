from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Dashboard, DashboardWidget, starter_dashboard
from .widgets import GRID_COLUMNS, WIDGET_REGISTRY


def _serialize(dashboard, request_user):
    """A dashboard plus its widgets, in the shape the API returns."""
    return {
        "id": str(dashboard.id),
        "name": dashboard.name,
        "is_default": dashboard.is_default,
        "shared": dashboard.shared,
        "owner": dashboard.owner.username,
        "is_mine": dashboard.owner_id == request_user.id,
        "widgets": [
            {
                "id": str(w.id),
                "kind": w.kind,
                "x": w.x,
                "y": w.y,
                "w": w.w,
                "h": w.h,
                "settings": w.settings,
            }
            for w in dashboard.widgets.all()
        ],
    }


def _visible_dashboards(user):
    """The caller's own dashboards, plus anything shared by anyone else."""
    qs = Dashboard.objects.filter(owner=user)
    if not qs.exists():
        starter_dashboard(user)
        qs = Dashboard.objects.filter(owner=user)
    shared = Dashboard.objects.exclude(owner=user).filter(shared=True)
    return list(qs) + list(shared)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def dashboard_list(request):
    """The caller's dashboards, plus any shared ones.

    A user with none is created a starter dashboard first, so the list is
    never empty.
    """
    if request.method == "GET":
        return Response([_serialize(d, request.user)
                         for d in _visible_dashboards(request.user)])

    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"error": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
    if len(name) > 120:
        return Response({"error": "name is too long (max 120)"},
                        status=status.HTTP_400_BAD_REQUEST)
    if Dashboard.objects.filter(owner=request.user, name=name).exists():
        return Response({"error": f"You already have a dashboard named {name!r}"},
                        status=status.HTTP_400_BAD_REQUEST)
    board = Dashboard.objects.create(
        owner=request.user, name=name,
        is_default=not Dashboard.objects.filter(owner=request.user).exists())
    return Response(_serialize(board, request.user), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def dashboard_detail(request, dashboard_id):
    """One dashboard with its widgets.

    A dashboard is visible to its owner, or to anyone when ``shared`` is true.
    Editing is owner-only — a 404 for a stranger's dashboard, a 403 when a
    non-owner writes to a shared one.
    """
    board = Dashboard.objects.filter(id=dashboard_id).first()
    if board is None:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    if board.owner_id != request.user.id and not board.shared:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_serialize(board, request.user))

    if board.owner_id != request.user.id:
        return Response({"error": "only the owner may edit this dashboard"},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == "PATCH":
        data = request.data
        name = data.get("name")
        if name is not None:
            name = str(name).strip()
            if not name:
                return Response({"error": "name must not be empty"},
                                status=status.HTTP_400_BAD_REQUEST)
            if len(name) > 120:
                return Response({"error": "name is too long (max 120)"},
                                status=status.HTTP_400_BAD_REQUEST)
            clash = (Dashboard.objects.filter(owner=request.user, name=name)
                     .exclude(pk=board.pk).exists())
            if clash:
                return Response({"error": f"You already have a dashboard named {name!r}"},
                                status=status.HTTP_400_BAD_REQUEST)
            board.name = name
        if "is_default" in data:
            board.is_default = bool(data["is_default"])
        board.save()
        board.refresh_from_db()
        return Response(_serialize(board, request.user))

    # DELETE
    board.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def layout_update(request, dashboard_id):
    """Replace every widget on a dashboard in one transaction.

    Takes ``{"widgets": [{"kind", "x", "y", "w", "h", "settings"}, …]}``.
    An unknown ``kind`` is a 400 naming it; ``w``/``h`` are clamped to the
    registry's minimums and to the grid width.
    """
    board = Dashboard.objects.filter(id=dashboard_id).first()
    if board is None or (board.owner_id != request.user.id and not board.shared):
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
    if board.owner_id != request.user.id:
        return Response({"error": "only the owner may edit this dashboard"},
                        status=status.HTTP_403_FORBIDDEN)

    widgets = request.data.get("widgets")
    if not isinstance(widgets, list):
        return Response({"error": "widgets must be a list"},
                        status=status.HTTP_400_BAD_REQUEST)

    planned = []
    for entry in widgets:
        if not isinstance(entry, dict):
            return Response({"error": "each widget must be an object"},
                            status=status.HTTP_400_BAD_REQUEST)
        kind = entry.get("kind")
        spec = WIDGET_REGISTRY.get(kind)
        if spec is None:
            return Response({"error": f"unknown widget kind: {kind!r}"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            x = int(entry.get("x", 0))
            y = int(entry.get("y", 0))
        except (TypeError, ValueError):
            return Response({"error": "x and y must be integers"},
                            status=status.HTTP_400_BAD_REQUEST)
        if x < 0 or y < 0:
            return Response({"error": "x and y must be non-negative"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            w = int(entry.get("w", spec["w"]))
            h = int(entry.get("h", spec["h"]))
        except (TypeError, ValueError):
            return Response({"error": "w and h must be integers"},
                            status=status.HTTP_400_BAD_REQUEST)
        w = max(spec["min_w"], min(w, GRID_COLUMNS))
        h = max(spec["min_h"], min(h, GRID_COLUMNS))
        planned.append((kind, x, y, w, h, entry.get("settings")))

    with transaction.atomic():
        board.widgets.all().delete()
        for kind, x, y, w, h, settings in planned:
            DashboardWidget.objects.create(
                dashboard=board, kind=kind, x=x, y=y, w=w, h=h, settings=settings)

    board.refresh_from_db()
    return Response(_serialize(board, request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def catalog(request):
    """The widget registry, plus the grid width layouts are expressed against."""
    return Response({"widgets": WIDGET_REGISTRY, "grid_columns": GRID_COLUMNS})
