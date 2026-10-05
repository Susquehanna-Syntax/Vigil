"""Read API for the software inventory. Scoped by site, never writable."""
import logging
from collections import Counter, defaultdict

from django.db.models import Count, F, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from vigil import scoping

from apps.hosts.models import Host

from .models import SoftwareItem, SoftwareSnapshot

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

_ROW_FIELDS = (
    "source", "package_id", "name", "version", "latest_version",
    "scope", "user", "managed", "first_seen",
)

# An item is outdated only when the source reported a newer version; an empty
# latest_version means "we don't know", not "current".
_OUTDATED = Q(latest_version__gt="") & ~Q(latest_version=F("version"))
_UNMANAGED = Q(managed=False)


def _scoped_items(user):
    """Software rows the user may see, on hosts that weren't rejected."""
    return scoping.filter_by_site(
        SoftwareItem.objects.exclude(host__status="rejected"),
        user,
        path="host__",
    )


def _row(base: dict, *, host_id=None, hostname=None, name_field="name",
         with_publisher=False) -> dict:
    out = {}
    if host_id is not None:
        out["host_id"] = str(host_id)
    if hostname is not None:
        out["hostname"] = hostname
    out["name"] = base[name_field]
    out["source"] = base["source"]
    out["package_id"] = base["package_id"]
    out["version"] = base["version"]
    out["latest_version"] = base["latest_version"]
    out["outdated"] = bool(base["latest_version"]) and base["latest_version"] != base["version"]
    out["scope"] = base["scope"]
    out["user"] = base["user"]
    out["managed"] = base["managed"]
    if with_publisher:
        out["publisher"] = base["publisher"]
    out["first_seen"] = base["first_seen"]
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def app_list(request):
    """One row per app (name_key) across the hosts the user may see."""
    qs = _scoped_items(request.user).values(
        "name_key", "name", "source", "host_id",
        "version", "latest_version", "managed", "package_id",
    )

    query = request.query_params.get("q", "").strip().lower()
    if query:
        qs = qs.filter(name_key__icontains=query)
    source = request.query_params.get("source", "").strip()
    if source:
        qs = qs.filter(source=source)

    # Per-app aggregates computed in Python over the scoped rows: "hosts"
    # counts distinct hosts, versions maps version → host count (a host with
    # two sources carrying the same version counts once).
    hosts = defaultdict(set)
    versions = defaultdict(lambda: defaultdict(set))
    outdated = defaultdict(set)
    unmanaged = defaultdict(set)
    names = defaultdict(Counter)
    sources = defaultdict(set)
    packages = defaultdict(set)

    for row in qs:
        key = row["name_key"]
        hosts[key].add(row["host_id"])
        versions[key][row["version"]].add(row["host_id"])
        if row["latest_version"] and row["latest_version"] != row["version"]:
            outdated[key].add(row["host_id"])
        if not row["managed"]:
            unmanaged[key].add(row["host_id"])
        names[key][row["name"]] += 1
        sources[key].add(row["source"])
        packages[key].add((row["source"], row["package_id"]))

    rows = []
    for key in hosts:
        rows.append({
            "name_key": key,
            "name": names[key].most_common(1)[0][0],
            "sources": sorted(sources[key]),
            # The inventory ids an app policy can name it by, per source.
            "packages": [{"source": s, "id": i} for s, i in sorted(packages[key])[:10]],
            "hosts": len(hosts[key]),
            "versions": {v: len(h) for v, h in versions[key].items()},
            "outdated": len(outdated[key]),
            "unmanaged": len(unmanaged[key]),
        })

    if request.query_params.get("outdated") == "1":
        rows = [r for r in rows if r["outdated"] > 0]
    if request.query_params.get("unmanaged") == "1":
        rows = [r for r in rows if r["unmanaged"] > 0]

    rows.sort(key=lambda r: (-r["hosts"], r["name_key"]))

    try:
        limit = int(request.query_params.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    return Response({"count": len(rows), "results": rows[offset:offset + limit]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def app_detail(request, name_key):
    """Every host row for one app, in scope."""
    rows = list(
        _scoped_items(request.user)
        .filter(name_key=name_key)
        .order_by("host__hostname")
        .values("host_id", "host__hostname", "source", "package_id", "name",
                "version", "latest_version", "scope", "user", "managed", "first_seen")
    )
    return Response({
        "name_key": name_key,
        "rows": [_row(r, host_id=r["host_id"], hostname=r["host__hostname"]) for r in rows],
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_software(request, host_id):
    """The host's snapshot plus its full item list."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

    snapshot = getattr(host, "software_snapshot", None)
    rows = list(
        SoftwareItem.objects.filter(host=host)
        .order_by("name_key", "source")
        .values(*_ROW_FIELDS, "publisher")
    )
    return Response({
        "snapshot": None if snapshot is None else {
            "digest": snapshot.digest,
            "collected_at": snapshot.collected_at,
            "received_at": snapshot.received_at,
            "item_count": snapshot.item_count,
            "errors": snapshot.errors,
        },
        "items": [_row(r, with_publisher=True) for r in rows],
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_summaries(request):
    """Per-host totals for the Apps page's *By host* tab.

    Every visible host is listed, including ones that never reported: zeros
    and a null `received_at` are the row an operator is looking for, so a
    missing snapshot must not hide the host. The counts come from one
    aggregate query, not one per host.
    """
    hosts = list(scoping.filter_by_site(
        Host.objects.exclude(status=Host.Status.REJECTED), request.user,
    ).order_by("hostname"))

    stats = {
        agg["host_id"]: agg
        for agg in SoftwareItem.objects.filter(host__in=hosts).values("host_id").annotate(
            item_count=Count("id"),
            outdated=Count("id", filter=_OUTDATED),
            unmanaged=Count("id", filter=_UNMANAGED),
        )
    }
    snapshots = {
        snap.host_id: snap
        for snap in SoftwareSnapshot.objects.filter(host__in=hosts)
    }

    rows = []
    for host in hosts:
        agg = stats.get(host.id, {})
        snapshot = snapshots.get(host.id)
        rows.append({
            "host_id": str(host.id),
            "hostname": host.hostname,
            "item_count": agg.get("item_count", 0),
            "outdated": agg.get("outdated", 0),
            "unmanaged": agg.get("unmanaged", 0),
            "received_at": snapshot.received_at if snapshot else None,
            "errors": snapshot.errors if snapshot else {},
        })
    return Response({"count": len(rows), "results": rows})
