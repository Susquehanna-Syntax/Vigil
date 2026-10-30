"""What a Jackil integration key may read: six kinds, read-only.

Search, resolve and detail all run a type through the same queryset function
below, so the three can never disagree about what the key's creator may see —
the failure mode that matters here is detail handing over a row that search
hid. Each finder copies the filter its own page uses (see apps/hosts/search.py).
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.tasks.authz import visible_runs
from vigil import licensing, scoping

from .integration_auth import IntegrationKeyAuthentication

#: The six kinds a key may read, in the order ``search`` returns them.
TYPES = ("host", "task", "playbook", "automation", "run", "device")

LIMIT_PER_TYPE = 8
MAX_QUERY = 100
MAX_IDS = 100

#: Freshness window for a host's headline CPU/memory numbers, and how far back
#: detail's short CPU series reaches. A check-in older than the window is not
#: a "current" number no matter which row happens to be latest.
LIVE_METRIC_WINDOW = timedelta(minutes=15)
CPU_SERIES_WINDOW = timedelta(hours=1)
CPU_SERIES_POINTS = 60

_NOT_FOUND = {"error": "Not found"}


# ── finders ───────────────────────────────────────────────────────────────────

def _hosts(user):
    from apps.hosts.models import Host

    return scoping.filter_by_site(
        Host.objects.exclude(status="rejected"), user).order_by("hostname")


def _tasks(user):
    from apps.tasks.models import TaskDefinition

    # Exactly search._tasks: the library's own rule, yours plus community
    # ones. Not site-scoped either, because a definition is not a row on a
    # host and the library page has never filtered it by site.
    return (TaskDefinition.objects.filter(archived_at__isnull=True)
            .filter(Q(owner=user) | Q(visibility=TaskDefinition.Visibility.COMMUNITY))
            .order_by("name"))


def _playbooks(user):
    from apps.playbooks.models import Playbook

    return scoping.filter_by_site(
        Playbook.objects.filter(archived_at__isnull=True), user,
        cascade_global=True).order_by("name")


def _automations(user):
    from apps.automations.models import Automation

    return scoping.filter_by_site(Automation.objects.all(), user,
                                  cascade_global=True).order_by("name")


def _runs(user):
    from apps.tasks.models import TaskRun

    # A run that touched a site the creator cannot see is hidden whole: its
    # summary covers every host it ran on (apps/tasks/authz.py).
    return visible_runs(TaskRun.objects.all(), user).order_by("-created_at")


def _devices(user):
    from apps.hosts.models import UnmanagedDevice

    # Devices are not site-scoped in Vigil today: every signed-in user reads
    # them, only admins write (phase 02 adds the per-key write flag).
    return UnmanagedDevice.objects.order_by("name")


#: type -> (queryset for a user, the fields search looks in).
_FINDERS = {
    "host": (_hosts, ("hostname", "os", "tags")),
    "task": (_tasks, ("name", "description")),
    "playbook": (_playbooks, ("name", "description")),
    "automation": (_automations, ("name",)),
    "run": (_runs, ("name_snapshot",)),
    "device": (_devices, ("name", "vendor", "location", "serial", "owner",
                          "mac_address")),
}


def _matching(kind: str, user, q: str):
    finder, fields = _FINDERS[kind]
    match = Q()
    for field in fields:
        match |= Q(**{f"{field}__icontains": q})
    return finder(user).filter(match)


def _owned(kind: str, user, ids):
    """The rows behind `ids` that `user` may see.

    An id that is malformed, missing or hidden is simply absent from the
    result: a UUID field raises on a non-UUID lookup, which would turn a
    helpdesk typo into a 500, and an error naming which id failed would be a
    hint about what exists.
    """
    qs = _FINDERS[kind][0](user)
    pk = qs.model._meta.pk
    wanted = []
    for candidate in ids:
        # The kind's own primary key decides what an id may be: a long run of
        # digits is not a UUID's int and must not reach a lookup that would
        # raise on it.
        try:
            value = pk.to_python(candidate)
            pk.get_prep_value(value)
        except (DjangoValidationError, TypeError, ValueError, OverflowError):
            continue
        wanted.append(value)
    return qs.filter(pk__in=wanted)


# ── presentation ──────────────────────────────────────────────────────────────

def deep_link(request, kind: str, pk) -> str:
    """The URL that opens a row in the SPA (`vigil-deeplink.js` honours it).
    Minted from the request so it carries whatever host the key is talking
    to; a relative link would be useless to a helpdesk agent."""
    return request.build_absolute_uri("/") + f"#open/{kind}/{pk}"


def _item(kind: str, obj, request) -> dict:
    """The row as the search page draws it: what to call it, what sits under
    that, and how loudly. ``url`` is the deep link phase 02 makes the SPA
    honour, so it is minted here rather than guessed at by the caller."""
    label, sub, status, tone = _HEADERS[kind](obj)
    return {
        "type": kind,
        "id": str(obj.pk),
        "label": label,
        "sub": sub,
        "status": status,
        "tone": tone,
        "url": deep_link(request, kind, obj.pk),
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _host_item(h):
    return (h.hostname, " · ".join(x for x in (h.os, h.ip_address or "") if x),
            h.get_status_display(),
            {"online": "ok", "offline": "bad"}.get(h.status, "warn"))


def _task_item(d):
    return (d.name, (d.description or "")[:80], d.get_risk_level_display(), "")


def _playbook_item(b):
    return b.name, (b.description or "")[:80], "", ""


def _automation_item(a):
    return (a.name, a.get_trigger_display(),
            "Enabled" if a.enabled else "Disabled",
            "ok" if a.enabled else "muted")


def _run_item(r):
    # The name the run was dispatched under wins: the definition may since
    # have been renamed, archived or deleted outright.
    name = (r.name_snapshot or (r.definition.name if r.definition else "")
            or r.get_source_display())
    sub = f"{r.created_at:%Y-%m-%d} · " + _plural(r.host_count, "host")
    tone = {"completed": "ok", "failed": "bad", "partial": "warn",
            "running": "info"}.get(r.state, "muted")
    return name, sub, r.get_state_display(), tone


def _device_item(d):
    sub = " · ".join(x for x in (d.get_device_type_display(), d.ip_address or "") if x)
    return d.name, sub, d.get_device_type_display(), ""


_HEADERS = {
    "host": _host_item,
    "task": _task_item,
    "playbook": _playbook_item,
    "automation": _automation_item,
    "run": _run_item,
    "device": _device_item,
}


def _dt(value) -> str:
    return "" if value is None else value.isoformat()


def _host_facts(h):
    from apps.alerts.models import Alert

    facts = [("OS", h.os), ("IP", h.ip_address or ""),
             ("Status", h.get_status_display()), ("Last seen", _dt(h.last_checkin))]
    live = _latest_metrics([h.id], timezone.now() - LIVE_METRIC_WINDOW)
    for label, category in (("CPU", "cpu"), ("Memory", "memory")):
        value = (live.get(category) or {}).get(h.id)
        if value is not None:
            facts.append((label, f"{round(value)}%"))
    open_alerts = h.alerts.filter(
        state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED]).count()
    facts.append(("Open alerts", str(open_alerts)))
    site = scoping.scope_of(h)
    if site is not None:
        facts.append(("Site", str(site)))
    return facts


def _cpu_series(h) -> list[dict]:
    """The host's CPU usage over the last hour, oldest first, at most
    CPU_SERIES_POINTS points — a busier series is thinned to evenly spaced
    points, first and last kept, rather than dropping its tail. Jackil draws
    this as its link preview."""
    points = list(h.metric_points
                  .filter(category="cpu", metric="usage_percent",
                          time__gte=timezone.now() - CPU_SERIES_WINDOW)
                  .order_by("time").values_list("time", "value"))
    if len(points) > CPU_SERIES_POINTS:
        last = len(points) - 1
        picks = sorted({round(i * last / (CPU_SERIES_POINTS - 1)) for i in range(CPU_SERIES_POINTS)})
        points = [points[i] for i in picks]
    return [{"t": moment.isoformat(), "v": round(value, 1)} for moment, value in points]


def _latest_metrics(host_ids, since) -> dict:
    """{"cpu": {host_id: value}, "memory": {...}} — the newest usage_percent
    point of each category at or after ``since``. Two aggregate queries plus
    two fill queries however many hosts are asked about; one query per host
    would put a list endpoint's budget on a slider."""
    from apps.metrics.models import MetricPoint

    out: dict[str, dict] = {}
    for category in ("cpu", "memory"):
        base = MetricPoint.objects.filter(category=category, metric="usage_percent")
        latest = (base.filter(host_id__in=list(host_ids), time__gte=since)
                  .values("host_id").annotate(time=Max("time")))
        times = {row["host_id"]: row["time"] for row in latest}
        values: dict = {}
        if times:
            for point in base.filter(host_id__in=list(times), time__gte=since).values(
                    "host_id", "time", "value"):
                if point["time"] == times.get(point["host_id"]):
                    values[point["host_id"]] = point["value"]
        out[category] = values
    return out


def _device_facts(d):
    facts = [("Type", d.get_device_type_display()), ("IP", d.ip_address or ""),
             ("MAC", d.mac_address), ("Vendor", d.vendor), ("Serial", d.serial),
             ("Location", d.location), ("Owner", d.owner),
             ("Warranty ends", d.warranty_end.isoformat() if d.warranty_end else "")]
    extra = d.extra if isinstance(d.extra, dict) else {}
    facts += [(key, extra[key]) for key in sorted(extra)]
    return facts


def _task_facts(d):
    return [("Risk", d.get_risk_level_display()),
            ("Owner", d.owner.username if d.owner is not None else "")]


def _playbook_facts(b):
    return [("Steps", b.steps.count()),
            ("Target tags", ", ".join(str(t) for t in (b.target_tags or [])))]


def _automation_facts(a):
    if a.task_definition_id:
        action = a.task_definition.name
    elif a.playbook_id:
        action = a.playbook.name
    else:
        action = a.get_action_kind_display()
    return [("Trigger", a.get_trigger_display()), ("Action", action),
            ("Enabled", "Yes" if a.enabled else "No")]


def _run_facts(r):
    return [("State", r.get_state_display()), ("Hosts", str(r.host_count)),
            ("Started", _dt(r.created_at)), ("Finished", _dt(r.finished_at))]


_FACTS = {
    "host": _host_facts,
    "task": _task_facts,
    "playbook": _playbook_facts,
    "automation": _automation_facts,
    "run": _run_facts,
    "device": _device_facts,
}


def _facts(kind: str, obj) -> list[dict]:
    """Detail's extra rows. An empty value is dropped rather than shown: a
    blank "Last seen" reads as a fact the server forgot, not as unknown."""
    return [{"label": label, "value": str(value)}
            for label, value in _FACTS[kind](obj) if str(value)]


def _scoped_key_auth():
    if not settings.BUSINESS_API_KEYS:
        return []
    from django.utils.module_loading import import_string
    return [import_string("apps_business.apikeys.auth.ScopedKeyAuthentication")]


# ── views ─────────────────────────────────────────────────────────────────────

def integration(view):
    """What every integration endpoint shares, in one place: GET only, and
    the key as the only credential — a session gets 401 here just as a key
    gets 401 everywhere else, so the two cannot be confused for each other.
    The policy decorators must sit inside ``api_view``, hence this order.

    A Business scoped API key is accepted too: it is how an admin limits
    what Jackil can see to some sites."""
    authentication_classes([IntegrationKeyAuthentication, *_scoped_key_auth()])(view)
    permission_classes([IsAuthenticated])(view)
    return api_view(["GET"])(view)


@integration
def ping(request):
    return Response({"ok": True, "version": settings.VIGIL_VERSION,
                     # What this Vigil's integration surface actually offers.
                     # Jackil checks these before it offers a feature, rather
                     # than 404ing against an older Vigil; phase 02 appends
                     # "device_write" for a key that may write devices.
                     "capabilities": ["devices", "host_facts"],
                     "acting_as": request.user.username})


@integration
def licence(request):
    """The signed licence and this instance's id, for Jackil to verify itself.

    Only the blob is handed over, never a verdict: Jackil checks the SQSY
    signature with its own copy of the public key, so a Vigil cannot grant
    Business by saying so.
    """
    try:
        return Response({"blob": licensing.load_blob()[0],
                         "instance": licensing.instance_id(),
                         "status": licensing.current_state().status.value})
    except Exception:  # noqa: BLE001 — licensing never takes anything down, not even this endpoint
        return Response({"blob": "", "instance": licensing.instance_id(),
                         "status": "none"})


@integration
def search(request):
    q = request.query_params.get("q", "").strip()[:MAX_QUERY]
    kind = request.query_params.get("type", "").strip()
    if kind and kind not in TYPES:
        return Response({"error": "Unknown type"}, status=400)
    if len(q) < 2:
        return Response({"results": []})
    results = []
    for name in (kind,) if kind else TYPES:
        for obj in _matching(name, request.user, q)[:LIMIT_PER_TYPE]:
            results.append(_item(name, obj, request))
    return Response({"results": results})


@integration
def resolve(request):
    kind = request.query_params.get("type", "").strip()
    if kind not in TYPES:
        return Response({"error": "Unknown type"}, status=400)
    ids = [one.strip() for one in request.query_params.get("ids", "").split(",")]
    ids = [one for one in ids if one]
    if len(ids) > MAX_IDS:
        return Response({"error": f"At most {MAX_IDS} ids per request"}, status=400)
    return Response({"items": {str(o.pk): _item(kind, o, request)
                               for o in _owned(kind, request.user, ids)}})


@integration
def detail(request, kind: str, id: str):
    if kind not in TYPES:
        return Response({"error": "Unknown type"}, status=400)
    obj = _owned(kind, request.user, [id]).first()
    if obj is None:
        # One body for "no such row" and "not yours to see", so probing ids
        # through a key cannot confirm a row the creator's site excludes.
        return Response(_NOT_FOUND, status=404)
    item = _item(kind, obj, request)
    if kind == "host":
        # Jackil draws a sparkline under the facts; the list responses stay
        # as lean as they were.
        item["cpu_series"] = _cpu_series(obj)
    item["facts"] = _facts(kind, obj)
    return Response(item)
