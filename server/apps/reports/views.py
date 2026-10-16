"""The reports API: saved selections and their computed chart data."""

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


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def report_index(request):
    if request.method == "GET":
        return Response([_row(report, request.user) for report in _visible_reports(request.user)])

    if not IsOperator().has_permission(request, None):
        return Response({"detail": IsOperator.message}, status=403)
    payload = _payload(request.data)
    problem = _validate(payload, request.user)
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
                      "description": spec["description"], "chart": spec["chart"]}
                     for kind, spec in widgets.WIDGETS.items()])


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

def _validate(payload, user, report=None, partial=False):
    """A 400 Response when the payload is not a report, else None.

    Only the fields the request carries are checked: a PATCH of one field must
    not re-reject a value the report was created with. A partial update also
    keeps its sources — clearing every one of them is what POST refuses, so it
    stays refused here too.
    """
    if "name" in payload:
        if not payload["name"]:
            return Response({"detail": "name is required"}, status=400)
        if len(payload["name"]) > 120:
            return Response({"detail": "name must be at most 120 characters"}, status=400)
    elif not partial:
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
        if spec.get("kind") not in widgets.WIDGETS:
            return f"unknown widget kind: {spec.get('kind')!r}"
        extra = set(spec) - {"kind", "title"}
        if extra:
            return f"unexpected widget keys: {sorted(extra)}"
        title = spec.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 80):
            return "widget title must be at most 80 characters"
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
