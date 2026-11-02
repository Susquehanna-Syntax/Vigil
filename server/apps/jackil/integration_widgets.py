"""Dashboard widget data, so a helpdesk can show Vigil without embedding Vigil.

Jackil draws a small dashboard of its own and asks Vigil for the numbers in
it. This is the read half of that: a catalogue of what can be asked for, and
one endpoint per widget returning an envelope in a fixed shape Jackil knows
how to draw — gauge, stat or list. No CSS, no markup: Jackil owns how a tile
looks, Vigil owns what is true.

The visibility rule is the one the pages use and the one phase 01's endpoints
use, applied as the key's creator: a West-only admin's dashboard counts West's
hosts and lists West's alerts. That is why every number below comes from a
queryset here rather than from a model-wide total.

The envelope is a contract and the receiving side rejects any key it does not
recognise, so ``data`` carries only the optional keys its shape allows, and a
value that does not exist is left out rather than sent as ``None``. The one
exception is the gauge's ``value``, where ``None`` means "there is nothing to
measure" and is the honest answer about a fleet with no reporting hosts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db.models import DateTimeField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.response import Response

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from vigil import scoping

from .integration_views import _hosts, _item, _runs, deep_link
from .integration_views import integration as _key_only


def integration(view):
    """Phase 01's decorator — GET only, the key as the only credential, so a
    session cannot reach a widget just as a key cannot reach a page. It is
    wrapped rather than stacked because DRF insists ``api_view`` sits above
    the policy decorators: one definition still, in ``integration_views``, so
    neither module can drift into accepting both credentials."""
    return _key_only(view)


#: Envelope version. Jackil refuses a payload it cannot match to a shape it
#: knows, so this is a handshake rather than decoration.
VERSION = 1

#: Every list setting means the same thing everywhere: how many rows fit on a
#: tile. Clamped rather than refused, because "give me 500" is a preference
#: and 20 is what a tile can draw.
LIMIT_MIN = 1
LIMIT_MAX = 20
LIMIT_DEFAULT = 8

#: The window behind "Failed runs (24h)".
FAILURE_WINDOW = timedelta(hours=24)

#: Sorts a host that has never checked in ahead of every dated one. The
#: sentinel is a date before the epoch: portable across SQLite and Postgres,
#: and never a check-in a real agent could have reported.
_NEVER_SEEN = Value(datetime(1970, 1, 1, tzinfo=dt_timezone.utc), DateTimeField())


class BadSetting(Exception):
    """A setting the caller passed that this widget cannot use."""


def _limit(request) -> int:
    """Row count from the query string. A non-number is a bad setting; a
    number out of range is the nearest one we can draw."""
    raw = request.query_params.get("limit", "")
    if raw == "":
        return LIMIT_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        raise BadSetting("limit must be a whole number")
    return max(LIMIT_MIN, min(LIMIT_MAX, value))


def _limit_setting(label: str) -> dict:
    return {"limit": {"type": "int", "label": label, "default": LIMIT_DEFAULT,
                      "min": LIMIT_MIN, "max": LIMIT_MAX}}


def _ok_when_zero(value: int, bad: bool) -> str:
    """Zero is quiet and non-zero is not: ``bad`` picks which of the two
    colours the non-zero end wears."""
    if value == 0:
        return "ok"
    return "bad" if bad else "warn"


# ── visibility ────────────────────────────────────────────────────────────────

def _open_alerts(user, *states):
    """Unresolved alerts, scoped exactly as the alerts page scopes them."""
    return scoping.filter_by_site(
        Alert.objects.filter(
            state__in=list(states) or [Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
        ).select_related("host"),
        user, path="host__")


# ── the single-number metrics ─────────────────────────────────────────────────

_METRICS = {
    "hosts_total": {
        "label": "Hosts",
        "count": lambda user: _hosts(user).count(),
        "tone": lambda value: "info",
    },
    "hosts_offline": {
        "label": "Offline hosts",
        "count": lambda user: _hosts(user).filter(status=Host.Status.OFFLINE).count(),
        "tone": lambda value: _ok_when_zero(value, bad=True),
    },
    "alerts_firing": {
        "label": "Firing alerts",
        "count": lambda user: _open_alerts(user, Alert.State.FIRING).count(),
        "tone": lambda value: _ok_when_zero(value, bad=False),
    },
    "alerts_critical": {
        "label": "Critical alerts",
        "count": lambda user: _open_alerts(user).filter(
            severity=AlertRule.Severity.CRITICAL).count(),
        "tone": lambda value: _ok_when_zero(value, bad=True),
    },
    "runs_failed_24h": {
        "label": "Failed runs (24h)",
        "count": lambda user: _runs(user).filter(
            state="failed", created_at__gte=timezone.now() - FAILURE_WINDOW).count(),
        "tone": lambda value: _ok_when_zero(value, bad=True),
    },
    "pending_enrollments": {
        "label": "Pending enrollments",
        "count": lambda user: scoping.filter_by_site(
            Host.objects.filter(status=Host.Status.PENDING), user).count(),
        "tone": lambda value: _ok_when_zero(value, bad=False),
    },
}

# ── the catalogue ─────────────────────────────────────────────────────────────
# Each entry names itself, says what it shows in words, declares the shape
# Jackil draws it as, lists the settings it takes, and points at the builder
# that fills the envelope's `data`.

WIDGETS = {
    "hosts_online": {
        "title": "Hosts online",
        "description": "The share of enrolled hosts reporting in.",
        "shape": "gauge",
        "settings": {},
        "build": lambda request, user, params: _hosts_online(user),
    },
    "stat": {
        "title": "A single number",
        "description": "One fleet count, coloured by whether it needs action.",
        "shape": "stat",
        "settings": {
            "metric": {
                "type": "choice",
                "label": "Metric",
                "default": "alerts_firing",
                "choices": [(name, metric["label"]) for name, metric in _METRICS.items()],
            },
        },
        "build": lambda request, user, params: _stat(user, params["metric"]),
    },
    "alert_list": {
        "title": "Open alerts",
        "description": "Alerts firing or acknowledged, newest first.",
        "shape": "list",
        "settings": _limit_setting("Rows"),
        "build": lambda request, user, params: _alert_list(user, request, params["limit"]),
    },
    "recent_runs": {
        "title": "Recent task runs",
        "description": "The newest runs the caller can see.",
        "shape": "list",
        "settings": _limit_setting("Rows"),
        "build": lambda request, user, params: _recent_runs(user, request, params["limit"]),
    },
    "offline_hosts": {
        "title": "Offline hosts",
        "description": "Hosts that stopped reporting, longest silence first.",
        "shape": "list",
        "settings": _limit_setting("Rows"),
        "build": lambda request, user, params: _offline_hosts(user, request, params["limit"]),
    },
}


# ── builders ──────────────────────────────────────────────────────────────────

def _hosts_online(user) -> dict:
    hosts = _hosts(user)
    online = hosts.filter(status=Host.Status.ONLINE).count()
    offline = hosts.filter(status=Host.Status.OFFLINE).count()
    # A fleet of pending and rejected hosts has no denominator: 0% would read
    # as an outage and 100% as a healthy fleet, and neither is true.
    if online + offline == 0:
        return {"value": None, "label": "Hosts online", "tone": "muted"}
    percent = round(online / (online + offline) * 100, 1)
    return {
        "value": percent,
        "label": "Hosts online",
        "sub": f"{online} of {online + offline} online",
        "tone": "ok" if percent >= 95 else "warn" if percent >= 80 else "bad",
    }


def _stat(user, metric: str) -> dict:
    if metric not in _METRICS:
        raise BadSetting("Unknown metric")
    row = _METRICS[metric]
    value = row["count"](user)
    return {"value": value, "label": row["label"], "tone": row["tone"](value)}


_SEVERITY_TONE = {"critical": "bad", "warning": "warn", "info": "info"}


def _alert_list(user, request, limit: int) -> dict:
    items = [{
        "title": f"{alert.host.hostname} · {alert.message[:80]}",
        "meta": f"{alert.get_severity_display()} · {_date(alert.fired_at)}",
        "badge": alert.get_state_display(),
        "tone": _SEVERITY_TONE.get(alert.severity, "info"),
        "href": deep_link(request, "host", alert.host_id),
    } for alert in _open_alerts(user).order_by("-fired_at")[:limit]]
    return {"items": items, "empty": "No open alerts."}


def _recent_runs(user, request, limit: int) -> dict:
    # Phase 01's row shape is what a run looks like everywhere else a key
    # reads Vigil; a tile that called the same run something else would be
    # the first thing a helpdesk agent disbelieved.
    items = []
    for run in _runs(user).order_by("-created_at")[:limit]:
        row = _item("run", run, request)
        items.append({"title": row["label"], "meta": row["sub"],
                      "badge": row["status"], "tone": row["tone"],
                      "href": row["url"]})
    return {"items": items, "empty": "No task runs yet."}


def _offline_hosts(user, request, limit: int) -> dict:
    # Oldest silence first — the host nobody has heard from for a month is
    # the one worth a tile. A host that never checked in has no date to sort
    # by and leads the list rather than sinking under a NULL: the sentinel is
    # a date before the epoch, portable across SQLite and Postgres and never
    # a real check-in.
    hosts = (_hosts(user).filter(status=Host.Status.OFFLINE)
             .annotate(quiet_since=Coalesce("last_checkin", _NEVER_SEEN))
             .order_by("quiet_since")[:limit])
    items = [{
        "title": host.hostname,
        "meta": (f"Last seen {_date(host.last_checkin)}" if host.last_checkin
                 else "Never seen"),
        "tone": "bad",
        "href": deep_link(request, "host", host.pk),
    } for host in hosts]
    return {"items": items, "empty": "Every host is online."}


def _date(value) -> str:
    return timezone.localtime(value).date().isoformat()


# ── endpoints ─────────────────────────────────────────────────────────────────

@integration
def widgets(request):
    """What can be asked for, and how to ask for it. Jackil builds its picker
    from this, so a new widget here needs no change over there."""
    return Response({
        "version": VERSION,
        "widgets": [{
            "kind": kind,
            "title": spec["title"],
            "description": spec["description"],
            "shape": spec["shape"],
            "settings": spec["settings"],
        } for kind, spec in WIDGETS.items()],
    })


@integration
def widget(request, kind: str):
    spec = WIDGETS.get(kind)
    if spec is None:
        return Response({"error": "Unknown widget"}, status=404)
    try:
        data = spec["build"](request, request.user, _settings(spec, request))
    except BadSetting as exc:
        return Response({"error": str(exc)}, status=400)
    return Response({"version": VERSION, "shape": spec["shape"], "data": data})


def _settings(spec: dict, request) -> dict:
    """The widget's settings read out of the query string, defaults applied.
    Two branches because there are exactly two setting types."""
    params = {}
    for name, meta in spec["settings"].items():
        raw = request.query_params.get(name, "")
        if meta["type"] == "choice":
            params[name] = raw if raw else meta["default"]
        else:
            params[name] = _limit(request)
    return params
