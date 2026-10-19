"""The reports API: saved selections and their computed chart data."""

import uuid

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsOperator
from apps.automations.models import Automation
from apps.playbooks.models import Playbook
from vigil import scoping

from . import widgets
from .models import Report

_FIELDS = ("name", "description", "range_days", "widgets", "playbooks", "automations")
#: The keys one widget spec may carry, and the four that place its card.
_WIDGET_KEYS = frozenset(("kind", "title", "chart", "x_label", "y_label", "legend",
                          "sources", "x", "y", "w", "h"))
_PLACE_KEYS = ("x", "y", "w", "h")


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def report_index(request):
    if request.method == "GET":
        return Response([_row(report, request.user) for report in _visible_reports(request.user)])

    if not IsOperator().has_permission(request, None):
        return Response({"detail": IsOperator.message}, status=403)
    payload = _payload(request.data)
    problem = _validate(payload, request.user, need_name=True)
    if problem:
        return problem
    report = Report.objects.create(
        name=payload["name"],
        description=payload.get("description", ""),
        owner=request.user,
        range_days=payload["range_days"],
        widgets=payload["widgets"],
    )
    _set_sources(report, payload, request.user)
    return Response(_row(report, request.user), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def report_detail(request, report_id):
    report = get_object_or_404(_visible_reports(request.user), pk=report_id)
    if request.method == "GET":
        return Response(_row(report, request.user))
    if not IsOperator().has_permission(request, None):
        return Response({"detail": IsOperator.message}, status=403)
    if request.method == "DELETE":
        report.delete()
        return Response(status=204)

    payload = _payload(request.data)
    problem = _validate(payload, request.user, report=report, partial=True)
    if problem:
        return problem
    for field in ("name", "description", "range_days", "widgets"):
        if field in payload:
            setattr(report, field, payload[field])
    report.save()
    _set_sources(report, payload, request.user)
    return Response(_row(report, request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def widget_catalog(request):
    return Response([{"kind": kind, "title": spec["title"],
                      "description": spec["description"], "chart": spec["chart"],
                      "family": spec["family"], "charts": spec["charts"],
                      "x_label": spec["x_label"], "y_label": spec["y_label"],
                      "how": spec["how"], "size": spec["size"],
                      "labels": {chart: list(widgets.default_labels(kind, chart))
                                 for chart in spec["charts"] if chart in widgets.AXIS_CHARTS}}
                     for kind, spec in widgets.WIDGETS.items()])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def report_preview(request):
    """Draw the widgets for an unsaved selection — the editor's live preview.

    The create rules, minus the name: the sources must still be ones the user
    can see, and they must choose at least one. A read-only user may preview,
    and nothing here is saved.
    """
    payload = _payload(request.data)
    problem = _validate(payload, request.user, need_name=False)
    if problem:
        return problem
    return Response(widgets.compute_for(
        _visible(Playbook.objects.filter(id__in=payload.get("playbooks") or []),
                 request.user),
        _visible(Automation.objects.filter(id__in=payload.get("automations") or []),
                 request.user),
        payload["range_days"], payload["widgets"], request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_data(request, report_id):
    report = get_object_or_404(_visible_reports(request.user), pk=report_id)
    return Response(widgets.compute(report, request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_data_csv(request, report_id, index):
    report = get_object_or_404(_visible_reports(request.user), pk=report_id)
    computed = widgets.compute(report, request.user)
    if not 0 <= index < len(computed["widgets"]):
        return Response({"detail": "no such widget"}, status=404)
    widget = computed["widgets"][index]
    response = HttpResponse(widgets.to_csv(widget), content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="{widgets.csv_filename(report, widget["kind"])}"'
    )
    return response


# -- serialisation -------------------------------------------------------------

def _visible_reports(user):
    """The reports *user* may open: ones they own, or that name at least one
    playbook or automation they can see. A report built from another site's
    sources is not theirs to read, rename or delete — it 404s."""
    from apps.accounts.permissions import visible_site_ids

    qs = Report.objects.all()
    if visible_site_ids(user) is None:
        return qs
    return qs.filter(
        Q(owner=user)
        | Q(playbooks__in=_visible(Playbook.objects.all(), user))
        | Q(automations__in=_visible(Automation.objects.all(), user))).distinct()


def _row(report, user) -> dict:
    playbooks = _ids(_visible(report.playbooks.all(), user))
    automations = _ids(_visible(report.automations.all(), user))
    return {
        "id": str(report.id),
        "name": report.name,
        "description": report.description,
        "range_days": report.range_days,
        "widgets": report.widgets,
        # Only the sources this user can see: another site's are not named.
        "playbooks": sorted(playbooks),
        "automations": sorted(automations),
        "owner": report.owner.username if report.owner_id else None,
        "updated_at": report.updated_at.isoformat(),
    }


def _set_sources(report, payload, user):
    """Set the sources the request names. Sources the editor cannot see were
    never shown to them, so an edit keeps them rather than dropping them."""
    for model, key in ((Playbook, "playbooks"), (Automation, "automations")):
        if key not in payload:
            continue
        field = getattr(report, key)
        hidden = field.exclude(pk__in=_visible(model.objects.all(), user).values("pk"))
        field.set(list(model.objects.filter(id__in=payload[key])) + list(hidden))


# -- validation ----------------------------------------------------------------

def _validate(payload, user, report=None, partial=False, need_name=True):
    """A 400 Response when the payload is not a report, else None.

    Only the fields the request carries are checked: a PATCH of one field must
    not re-reject a value the report was created with. A partial update also
    keeps its sources — clearing every one of them is what POST refuses, so it
    stays refused here too. ``need_name=False`` is a preview, which has no name
    and nothing to keep.
    """
    # A preview has no name, but it still needs a range and widgets to draw.
    if "name" in payload:
        if not payload["name"]:
            return Response({"detail": "name is required"}, status=400)
        if len(payload["name"]) > 120:
            return Response({"detail": "name must be at most 120 characters"}, status=400)
    elif not partial and need_name:
        return Response({"detail": "name is required"}, status=400)

    if "range_days" in payload:
        if payload["range_days"] not in Report.RANGES:
            return Response(
                {"detail": f"range_days must be one of {list(Report.RANGES)}"}, status=400)
    elif not partial:
        return Response({"detail": "range_days is required"}, status=400)

    if "widgets" in payload:
        problem = _widget_errors(payload["widgets"])
        if problem:
            return Response({"detail": problem}, status=400)
    elif not partial:
        return Response({"detail": "widgets are required"}, status=400)

    for model, key, detail in ((Playbook, "playbooks", "unknown playbook"),
                               (Automation, "automations", "unknown automation")):
        if key not in payload:
            continue
        if not isinstance(payload[key], list):
            return Response({"detail": f"{key} must be a list of ids"}, status=400)
        wanted = _ids(payload[key])
        visible = _ids(_visible(model.objects.all(), user))
        if wanted - visible:
            return Response({"detail": detail}, status=400)

    if not any(_sources_of(payload, report, partial)):
        return Response({"detail": "a report needs at least one playbook or automation"},
                        status=400)
    return None


def _sources_of(payload, report, partial=False) -> tuple:
    def current(key):
        if key in payload:
            return list(payload[key])
        if report is not None and partial:
            return list(getattr(report, key).values_list("id", flat=True))
        return []

    return current("playbooks"), current("automations")


def _ids(rows) -> set:
    """Identities of ids or model rows, so a request and a queryset compare."""
    return {str(getattr(row, "pk", row)) for row in rows}


def _widget_errors(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        return "widgets must be a list of 1 to 12 entries"
    for spec in raw:
        if not isinstance(spec, dict):
            return "each widget must be an object"
        kind = spec.get("kind")
        if kind not in widgets.WIDGETS:
            return f"unknown widget kind: {kind!r}"
        extra = set(spec) - _WIDGET_KEYS
        if extra:
            return f"unexpected widget keys: {sorted(extra)}"
        title = spec.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 80):
            return "widget title must be at most 80 characters"
        problem = _chart_error(spec, kind)
        if problem:
            return problem
    return None


def _chart_error(spec, kind):
    """The widget's drawing options, labels, sources and place, one error each.

    Every key here is optional; a widget naming none of them draws and places
    exactly as it did before they existed.
    """
    defaults = widgets.WIDGETS[kind]
    if "chart" in spec and spec["chart"] not in defaults["charts"]:
        return f"chart {spec['chart']!r} does not suit {kind}"
    for key in ("x_label", "y_label"):
        if key in spec:
            label = spec[key]
            if not isinstance(label, str) or len(label) > 40:
                return "axis labels must be text of at most 40 characters"
    if "legend" in spec and spec["legend"] not in (True, False, None):
        return "legend must be true, false or null"
    if "sources" in spec and not _is_id_list(spec["sources"]):
        return "sources must be a list of playbook or automation ids"
    return _place_error(spec)


def _is_id_list(value) -> bool:
    if not isinstance(value, list) or len(value) > 50:
        return False
    return all(_is_uuid(item) for item in value)


def _is_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _place_error(spec):
    placed = [key for key in _PLACE_KEYS if key in spec]
    if len(placed) != 4:
        return ("a widget's x, y, w and h go together" if placed else None)
    for key in _PLACE_KEYS:
        if not isinstance(spec[key], int) or isinstance(spec[key], bool):
            return "a widget's place must fit a 12-column grid"
    x, y, w, h = (spec[key] for key in _PLACE_KEYS)
    if not (0 <= x <= 11 and 0 <= y <= 500 and 1 <= w and x + w <= 12 and 2 <= h <= 12):
        return "a widget's place must fit a 12-column grid"
    return None


def _payload(data) -> dict:
    """Only the fields the request carries, so PATCH stays partial."""
    out = {key: data[key] for key in _FIELDS if key in data}
    if out.get("name") is not None:
        out["name"] = str(out["name"]).strip()
    if "description" in out:
        out["description"] = (out["description"] or "").strip()
    return out


def _visible(qs, user):
    return scoping.filter_by_site(qs, user, cascade_global=True)
