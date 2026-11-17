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
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from django.db.models import Q
from django.utils.timezone import now

from vigil.scoping import site_id_of
from vigil.signing import get_signing_key

from .crypto import decrypt_secret
from .models import CheckResult, DeviceCheck, Host

# The same host rule as agent/vigil_agent/netcheck.py (_HOST_RE, _valid_host, MAX_HOST_LENGTH).
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")
# A dotted numeric OID, and the lower-case label an extra result row is keyed by.
_OID_RE = re.compile(r"[0-2](\.(0|[1-9][0-9]{0,9})){1,127}")
_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_SNMP_OIDS = 8
# A labelled OID must not shadow a key _snmp already puts in its own result.
RESERVED_OID_LABELS = frozenset({"sys_name", "uptime_s", "check", "ok", "latency_ms", "detail",
                                 "at", "kind", "target", "interfaces",
                                 "version", "user", "auth", "priv", "community",
                                 "auth_password", "priv_password"})
MAX_HOST_LENGTH = 253
MIN_INTERVAL = 30
MAX_INTERVAL = 86400
MAX_RESULTS_PER_CHECKIN = 500
MAX_DETAIL = 300
MAX_EXTRA_KEYS = 20
MAX_SAMPLES_PER_RESULT = 64
MAX_IF_INDEX = 2147483647          # an ifIndex is a 32-bit counter, so this is its top
MAX_COUNTER = 9223372036854775807
MAX_SAMPLE_NAME = 100
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


def _bool_value(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def _snmp_user(value) -> str:
    """An SNMPv3 user name: 1 to 32 bytes of UTF-8 with no control characters.

    The length is counted in bytes because that is what goes on the wire as the
    USM security name; a two-byte character therefore buys two of the 32.
    """
    if isinstance(value, bool) or not isinstance(value, str) \
            or any(unicodedata.category(c) == "Cc" for c in value) \
            or not 1 <= len(value.encode("utf-8")) <= 32:
        raise ValueError("user must be 1 to 32 characters")
    return value


def _snmp_params(params: dict) -> dict:
    """Cleaned SNMP params. The secrets are not params keys (see views._check_fields)."""
    cleaned = {"host": _host_value(params.get("host"), "host")}
    port = _int_in(params.get("port", 161), "port", 1, 65535)
    if port != 161:
        cleaned["port"] = port
    version = params.get("version", "2c")
    if version == "3":
        cleaned["version"] = "3"
        cleaned["user"] = _snmp_user(params.get("user"))
        auth = params.get("auth")
        if auth not in ("sha", "sha256"):
            raise ValueError("auth must be sha or sha256")
        cleaned["auth"] = auth
        # `priv` says whether the check encrypts. It is not a secret, so it is
        # stored; check_params_for drops it again before a poller sees it.
        cleaned["priv"] = _bool_value(params.get("priv", True), "priv")
    elif version != "2c":
        # v2c is the default and stays stored as it has always been: no `version` key.
        raise ValueError("version must be 2c or 3")
    raw_oids = params.get("oids", [])
    if not isinstance(raw_oids, list):
        raise ValueError("oids must be a list of {oid, label}")
    if len(raw_oids) > MAX_SNMP_OIDS:
        raise ValueError("oids: at most 8")
    oids = []
    labels = set()
    for item in raw_oids:
        if not isinstance(item, dict) or not isinstance(item.get("oid"), str) \
                or not isinstance(item.get("label"), str):
            raise ValueError("oids must be a list of {oid, label}")
        oid, label = item["oid"], item["label"]
        if not _OID_RE.fullmatch(oid):
            raise ValueError(f"oids: {oid} is not a numeric OID")
        if not _LABEL_RE.fullmatch(label):
            raise ValueError("oids: labels are lower-case letters, digits and _")
        if label in RESERVED_OID_LABELS or label in labels:
            raise ValueError(f"oids: {label} is a reserved label")
        labels.add(label)
        oids.append({"oid": oid, "label": label})
    if oids:
        cleaned["oids"] = oids
    if "interfaces" in params:
        if not isinstance(params["interfaces"], bool):
            raise ValueError("interfaces must be true or false")
        if params["interfaces"]:
            cleaned["interfaces"] = True
    return cleaned


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
    if kind == DeviceCheck.Kind.SNMP:
        # The community and the SNMPv3 passwords are top-level write fields
        # stored encrypted on the model; params never carry a secret, so drop
        # any that arrive here.
        return _snmp_params({k: v for k, v in params.items()
                             if k not in ("community", "auth_password", "priv_password")})
    raise ValueError("kind must be icmp, tcp, http, dns or snmp")


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


def _stored_v3_secret(check: DeviceCheck) -> dict | None:
    """The SNMPv3 passwords of a stored check's secret, or None.

    A secret that does not decrypt, does not parse as this JSON object, or is
    not shaped like one counts as none — the same "as good as gone" rule an
    undecryptable community follows in check_params_for.
    """
    plaintext = decrypt_secret(check.secret)
    if not plaintext:
        return None
    try:
        stored = json.loads(plaintext)
    except (ValueError, TypeError):
        return None
    return stored if isinstance(stored, dict) else None


def check_params_for(check: DeviceCheck) -> dict | None:
    """The params this check is handed to a poller with, or None if it cannot run.

    For an SNMP check that is a *copy* of the stored params plus the decrypted
    secret — the community for v2c, the SNMPv3 passwords for v3; nothing here is
    ever written back to the model. None for a check whose secret no longer
    decrypts or lacks a password its params ask for (SECRET_KEY rotated, a
    secret written by another kind): the caller leaves such a check out rather
    than send a poller a check it cannot authenticate with. ``priv`` is a stored
    param but not a wire one — an authNoPriv v3 entry carries no privacy
    password and says nothing else about privacy.
    """
    if check.kind != DeviceCheck.Kind.SNMP:
        return check.params
    params = dict(check.params)
    if params.get("version") != "3":
        community = decrypt_secret(check.secret)
        if not community:
            return None
        params["community"] = community
        return params
    stored = _stored_v3_secret(check)
    if stored is None:
        return None
    auth_password = stored.get("auth")
    if not isinstance(auth_password, str):
        return None
    params["version"] = 3
    params["auth_password"] = auth_password
    if params.pop("priv", True):
        priv_password = stored.get("priv")
        if not isinstance(priv_password, str):
            return None
        params["priv_password"] = priv_password
    return params


def signed_check_list(host: Host) -> dict:
    entries = []
    for c in checks_for(host):
        params = check_params_for(c)
        if params is None:
            continue
        entries.append({"id": str(c.id), "kind": c.kind, "params": params,
                        "interval": c.interval_seconds})
    payload = {"v": 1, "type": "check-list", "agent": host.token_fingerprint,
               "issued": int(time.time()),
               "checks": entries}
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


def _sample_int(value) -> int | None:
    """A counter as stored, or None. A Counter64 past the signed range is dropped."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MAX_COUNTER:
        return None
    return value


def _interface_samples(check: DeviceCheck, sample_time, interfaces) -> list:
    """One InterfaceSample per interface an SNMP result carries.

    A malformed entry is dropped rather than refused with it: one bad row from
    a device would otherwise cost the check every interface it does report.
    """
    from .models import InterfaceSample

    samples = []
    seen: set[int] = set()
    for entry in interfaces[:MAX_SAMPLES_PER_RESULT]:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if not 1 <= index <= MAX_IF_INDEX or index in seen:
            continue
        seen.add(index)
        name = entry.get("name")
        up = entry.get("up")
        samples.append(InterfaceSample(
            device_check=check, time=sample_time, if_index=index,
            name=name[:MAX_SAMPLE_NAME] if isinstance(name, str) else "",
            up=up if isinstance(up, bool) else None,
            speed_mbps=_sample_int(entry.get("speed_mbps")),
            in_octets=_sample_int(entry.get("in_octets")),
            out_octets=_sample_int(entry.get("out_octets"))))
    return samples


def store_check_results(host: Host, rows) -> int:
    """Store the results a poller sent. Only results for checks it is assigned now are kept.

    An SNMP result carrying an ``interfaces`` list feeds InterfaceSample rows
    with the same time as its CheckResult, so the rate between two runs is the
    distance between two rows the device answered at one instant.
    """
    from .models import InterfaceSample

    if not isinstance(rows, list):
        return 0
    allowed = {str(c.id): c for c in checks_for(host)}
    to_create = []
    samples = []
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
        if check.kind == DeviceCheck.Kind.SNMP and isinstance(row.get("interfaces"), list):
            samples.extend(_interface_samples(check, to_create[-1].time, row["interfaces"]))
    CheckResult.objects.bulk_create(to_create)
    InterfaceSample.objects.bulk_create(samples)
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
