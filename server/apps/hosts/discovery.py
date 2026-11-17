"""Discovery scans for agent-less networks (S9 06b): the signed hand-off and the answer.

A poller is handed at most one scan, the oldest queued or sent one of its own,
inside its check-in response and signed with the task key like the device-check
list (vigil.signing). The answer — found hosts, or an error — comes back in a
later check-in and is stored here. Nothing schedules any of it: a check-in is
what times a scan out as well as what hands it out.
"""

import base64
import ipaddress
import json
import math
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from vigil import scoping
from vigil.signing import get_signing_key

from .crypto import decrypt_secret
from .models import DiscoveredHost, DiscoveryScan, Host

#: How long a poller may hold a scan without answering before it fails.
SCAN_ANSWER_TIMEOUT = timedelta(minutes=30)
#: Found hosts stored per scan; the agent sends at most this many anyway.
MAX_FOUND = 1024
MAX_NAME = 100
MAX_PORTS_PER_HOST = 16
NO_ANSWER = "The poller did not answer."
BAD_COMMUNITY = "The SNMP community could not be read; enter it again."


def _fail(scan: DiscoveryScan, error: str) -> None:
    scan.state = DiscoveryScan.State.FAILED
    scan.error = error[:200]
    scan.finished_at = now()
    scan.save(update_fields=["state", "error", "finished_at"])


def signed_scan(host: Host) -> dict:
    """The ``scan`` keys of this poller's check-in response, or {} when it has none.

    A scan the poller never answered fails first and is not handed out again;
    a queued scan becomes ``sent`` as it leaves. Only this poller's own scans
    are ever read or written — an agent cannot be steered onto another poller's
    work by naming its id.
    """
    if not host.agent_poller:
        return {}
    # Instance-wide like checks_for: the agent is authenticated by its own
    # token, and a check-in answers for one poller across every site (a device
    # or scan of another site must still be reachable to be handed out).
    with scoping.system(reason="agent check-in: this poller's own scans, whichever site made it"):
        current = now()
        stale = list(DiscoveryScan.objects.filter(
            poller=host, state=DiscoveryScan.State.SENT,
            sent_at__lt=current - SCAN_ANSWER_TIMEOUT))
        for scan in stale:
            _fail(scan, NO_ANSWER)
        scan = (DiscoveryScan.objects.filter(
            poller=host, state__in=[DiscoveryScan.State.QUEUED, DiscoveryScan.State.SENT])
            .order_by("created_at", "id").first())
        if scan is None:
            return {}
        if scan.state == DiscoveryScan.State.QUEUED:
            scan.state = DiscoveryScan.State.SENT
            scan.sent_at = current
            scan.save(update_fields=["state", "sent_at"])
        entry = {"id": str(scan.id), "cidr": scan.cidr, "ports": scan.ports}
        if scan.secret:
            community = decrypt_secret(scan.secret)
            if not community:
                # SECRET_KEY rotated since the scan was queued: fail it rather
                # than send a poller a scan it cannot finish, and say nothing
                # of the community itself.
                _fail(scan, BAD_COMMUNITY)
                return {}
            entry["community"] = community
    payload = {"v": 1, "type": "discovery-scan", "agent": host.token_fingerprint,
               "issued": int(time.time()), "scan": entry}
    signature = base64.b64encode(
        get_signing_key().sign(json.dumps(payload, sort_keys=True).encode()).signature
    ).decode()
    return {"scan": entry, "scan_issued": payload["issued"], "scan_sig": signature}


def _latency(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _ports(raw) -> list:
    if not isinstance(raw, list):
        return []
    ports = {p for p in raw if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535}
    return sorted(ports)[:MAX_PORTS_PER_HOST]


def _text(value) -> str:
    """A name a device reported: printable characters only, cut to MAX_NAME."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:MAX_NAME]


def store_scan_result(host: Host, data) -> bool:
    """Store what a poller answered a scan with. Returns whether anything was stored.

    The scan must be this poller's own and still ``sent``; a scan already done
    or failed keeps its answer, so a repeat send cannot duplicate its rows.
    """
    if not isinstance(data, dict):
        return False
    try:
        scan_id = uuid.UUID(data.get("id"))
    except (TypeError, ValueError, AttributeError):   # not a string, or not a UUID
        return False
    with scoping.system(reason="agent check-in: this poller's own scan answer"), \
            transaction.atomic():
        scan = DiscoveryScan.objects.filter(poller=host, id=scan_id).first()
        if scan is None or scan.state != DiscoveryScan.State.SENT:
            return False
        # Parsed once: a cidr that is not a network answers no rows at all.
        try:
            network = ipaddress.ip_network(scan.cidr)
        except ValueError:
            network = None
        error = data.get("error")
        if isinstance(error, str):
            _fail(scan, error)
            return True
        rows = []
        seen: set[str] = set()
        found = data.get("found")
        for entry in (found if isinstance(found, list) else [])[:MAX_FOUND]:
            if not isinstance(entry, dict):
                continue
            raw_ip = entry.get("ip")
            if not isinstance(raw_ip, str):
                continue
            try:
                address = ipaddress.IPv4Address(raw_ip)
            except ValueError:      # a bad octet, or an empty one
                continue
            # A poller that answers outside the range it was given is lying
            # about a network it was never asked about; so is a repeated ip.
            if network is None or address not in network:
                continue
            text = str(address)
            if text in seen:
                continue
            seen.add(text)
            alive_by = entry.get("alive_by")
            rows.append(DiscoveredHost(
                scan=scan, ip=text,
                alive_by=alive_by if alive_by in ("ping", "tcp") else "tcp",
                latency_ms=_latency(entry.get("latency_ms")),
                open_ports=_ports(entry.get("open_ports")),
                name=_text(entry.get("name")),
                sys_name=_text(entry.get("sys_name")),
                sys_descr=_text(entry.get("sys_descr"))))
        DiscoveredHost.objects.bulk_create(rows)
        raw = data.get("scanned")
        scanned = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        scan.state = DiscoveryScan.State.DONE
        scan.scanned = max(0, scanned)
        scan.truncated = data.get("truncated") if isinstance(data.get("truncated"), bool) else False
        scan.finished_at = now()
        scan.save(update_fields=["state", "scanned", "truncated", "finished_at"])
    return True
