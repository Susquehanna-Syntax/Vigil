"""Checks on agent-less devices (S9): validation, assignment to a poller, signed hand-off, results.

A poller is an agent whose own agent.yml says ``poller: true`` (``Host.agent_poller``). The
server gives each enabled DeviceCheck to exactly one poller of the device's site, inside that
poller's check-in response, signed with the task key like the relay list (vigil.signing). The
poller's results come back in its next check-in and are stored as CheckResult rows.
"""

import base64
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from django.db.models import Q
from django.utils.timezone import now

from vigil.scoping import site_id_of
from vigil.signing import get_signing_key

from .models import CheckResult, DeviceCheck, Host

# The same host rule as agent/vigil_agent/netcheck.py (_HOST_RE, _valid_host, MAX_HOST_LENGTH).
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")
MAX_HOST_LENGTH = 253
MIN_INTERVAL = 30
MAX_INTERVAL = 86400
MAX_RESULTS_PER_CHECKIN = 500
MAX_DETAIL = 300
MAX_EXTRA_KEYS = 20
MAX_RESULT_AGE = timedelta(hours=1)
_RESULT_KEYS = {"check", "ok", "latency_ms", "detail", "at"}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _host_value(value, label: str) -> str:
    if not isinstance(value, str) or not (0 < len(value) <= MAX_HOST_LENGTH) \
            or value.startswith("-") or _HOST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a host name or address")
    return value


def _int_in(value, label: str, low: int, high: int) -> int:
    if not _is_int(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}")
    return value


def _bool_value(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def validate_interval(value) -> int:
    """interval_seconds: a whole number of seconds from 30 to 86400."""
    return _int_in(value, "interval_seconds", MIN_INTERVAL, MAX_INTERVAL)


def validate_params(kind: str, params) -> dict:
    """Cleaned params for one check kind, or ValueError with one sentence. Unknown keys are dropped."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if kind == DeviceCheck.Kind.ICMP:
        cleaned = {"host": _host_value(params.get("host"), "host")}
        if "count" in params:
            cleaned["count"] = _int_in(params["count"], "count", 1, 10)
        return cleaned
    if kind == DeviceCheck.Kind.TCP:
        return {"host": _host_value(params.get("host"), "host"),
                "port": _int_in(params.get("port"), "port", 1, 65535)}
    if kind == DeviceCheck.Kind.HTTP:
        url = params.get("url")
        if not isinstance(url, str):
            raise ValueError("url must be an http or https address")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must be an http or https address with a host")
        _host_value(parts.hostname, "url host")
        if parts.username or parts.password:
            # Check params are shown to everyone who can see the device; a
            # password in the URL would be shown with them.
            raise ValueError("url must not carry a user name or password")
        cleaned = {"url": url}
        if "expect_status" in params:
            codes = params["expect_status"]
            if not isinstance(codes, list) or not codes:
                raise ValueError("expect_status must be a non-empty list of status codes")
            cleaned["expect_status"] = [_int_in(c, "expect_status", 100, 599) for c in codes]
        if "verify_tls" in params:
            cleaned["verify_tls"] = _bool_value(params["verify_tls"], "verify_tls")
        if "expect_cert_days" in params:
            cleaned["expect_cert_days"] = _int_in(params["expect_cert_days"],
                                                  "expect_cert_days", 0, 365)
        return cleaned
    if kind == DeviceCheck.Kind.DNS:
        cleaned = {"name": _host_value(params.get("name"), "name")}
        if "expect" in params:
            expect = params["expect"]
            if not isinstance(expect, list) or not all(isinstance(e, str) for e in expect):
                raise ValueError("expect must be a list of strings")
            cleaned["expect"] = expect
        return cleaned
    raise ValueError("kind must be icmp, tcp, http or dns")


def _eligible(poller: Host, device) -> bool:
    """Online, opted in as a poller, and in exactly the device's site.

    A global device goes only to a global poller: every site can see it, so a
    poller one site's admins control must not be the one reporting on it. On a
    Community install everything is global, so any poller qualifies."""
    if poller.status != Host.Status.ONLINE or not poller.agent_poller:
        return False
    return site_id_of(poller) == device.site_id


def _assign(check: DeviceCheck, pollers: list) -> Host | None:
    """The poller for one check, given the online pollers in id order."""
    device = check.device
    if check.poller_id is not None and check.poller is not None and _eligible(check.poller, device):
        return check.poller
    for poller in pollers:
        if _eligible(poller, device):
            return poller
    return None


def assigned_poller(check: DeviceCheck) -> Host | None:
    """The poller that runs this check now: its own poller when set and eligible, else the
    lowest-id eligible poller of the device's site, else None."""
    pollers = list(Host.objects.filter(status=Host.Status.ONLINE, agent_poller=True).order_by("id"))
    return _assign(check, pollers)


def checks_for(host: Host) -> list[DeviceCheck]:
    """Every enabled check this host is the assigned poller for, sorted by id.

    Evaluated in Python over the enabled checks of this host's own site (global devices for a
    global poller).
    That is O(checks) per call, made once per check-in, and fine below a few thousand checks.
    """
    if not host.agent_poller:
        return []
    host_site = site_id_of(host)
    # Exactly this host's site: a global poller serves global devices, a site's
    # poller serves that site's (see _eligible).
    devices = Q(device__site_id__isnull=True) if host_site is None else Q(device__site_id=host_site)
    checks = (DeviceCheck.objects.filter(enabled=True).filter(devices)
              .select_related("device", "poller"))
    pollers = list(Host.objects.filter(status=Host.Status.ONLINE, agent_poller=True).order_by("id"))
    mine = [c for c in checks if (p := _assign(c, pollers)) is not None and p.id == host.id]
    return sorted(mine, key=lambda c: str(c.id))


def signed_check_list(host: Host) -> dict:
    payload = {"v": 1, "type": "check-list", "agent": host.token_fingerprint,
               "issued": int(time.time()),
               "checks": [{"id": str(c.id), "kind": c.kind, "params": c.params,
                           "interval": c.interval_seconds} for c in checks_for(host)]}
    signature = base64.b64encode(
        get_signing_key().sign(json.dumps(payload, sort_keys=True).encode()).signature
    ).decode()
    return {"checks": payload["checks"], "checks_issued": payload["issued"], "checks_sig": signature}


def _number_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _result_time(raw):
    current = now()
    if not isinstance(raw, str):
        return current
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return current
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if current - MAX_RESULT_AGE <= parsed <= current:
        return parsed
    return current


def _extra(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key in _RESULT_KEYS or len(out) >= MAX_EXTRA_KEYS:
            continue
        if value is None or isinstance(value, (str, bool, int)):
            out[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            out[key] = value
    return out


def store_check_results(host: Host, rows) -> int:
    """Store the results a poller sent. Only results for checks it is assigned now are kept."""
    if not isinstance(rows, list):
        return 0
    allowed = {str(c.id): c for c in checks_for(host)}
    to_create = []
    for row in rows[:MAX_RESULTS_PER_CHECKIN]:
        if not isinstance(row, dict):
            continue
        cid = row.get("check")
        check = allowed.get(cid) if isinstance(cid, str) else None
        if check is None or not isinstance(row.get("ok"), bool):
            continue
        detail = row.get("detail")
        to_create.append(CheckResult(
            device_check=check,
            time=_result_time(row.get("at")),
            ok=row["ok"],
            latency_ms=_number_or_none(row.get("latency_ms")),
            detail=(detail if isinstance(detail, str) else "")[:MAX_DETAIL],
            extra=_extra(row),
        ))
    CheckResult.objects.bulk_create(to_create)
    if to_create:
        # A check that just failed twice opens an alert (S9 04b), in the worker.
        # Late import: alerts imports hosts models, so a module-level import
        # would be circular.
        from apps.alerts.device_alerts import queue_evaluation
        queue_evaluation({r.device_check_id for r in to_create})
    return len(to_create)


def device_states(devices) -> dict:
    """``check_state`` for a page of devices: up, down, or null.

    ``"down"`` when any enabled check's latest result failed, ``"up"`` when
    every enabled check that has reported succeeded, ``null`` when the device
    has no enabled checks or none of them has ever reported. Disabled checks
    say nothing: a check nobody runs is not evidence either way.

    One query for the whole list: the enabled checks of these devices, each
    carrying its newest result's ``ok`` as a Subquery. A device with no checks
    is absent from the answer; callers read an absent device as None.
    """
    ids = [d.id for d in devices]
    if not ids:
        return {}

    from django.db.models import BooleanField, OuterRef, Subquery

    from .models import CheckResult, DeviceCheck

    latest_ok = (CheckResult.objects.filter(device_check=OuterRef("pk"))
                 .order_by("-time", "-id").values("ok")[:1])
    rows = (DeviceCheck.objects.filter(enabled=True, device_id__in=ids)
            .annotate(latest_ok=Subquery(latest_ok, output_field=BooleanField()))
            .values_list("device_id", "latest_ok"))

    out: dict = {}
    for device_id, ok in rows:
        if ok is None:          # never reported
            out.setdefault(device_id, None)
        elif not ok:
            out[device_id] = "down"
        elif out.get(device_id) != "down":
            out[device_id] = "up"
    return out
