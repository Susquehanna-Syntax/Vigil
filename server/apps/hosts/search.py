"""Search everything (QA-25) — one box over what the user may already see.

Each kind is filtered exactly the way its own page filters it: hosts and
everything on a host through ``scoping.filter_by_site``, stacks and update
policies for admins only (their APIs are admin-only), task definitions the
user's own plus community ones (what the task library lists).
"""
import re

from django.db.models import Count, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin

from vigil import scoping

LIMIT = 8
MAX_QUERY = 100
_IP_PREFIX = re.compile(r"^[0-9a-fA-F:.]+$")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _hosts(request, q):
    from .models import Host
    match = Q(hostname__icontains=q) | Q(os__icontains=q) | Q(tags__icontains=q)
    if _IP_PREFIX.match(q):
        match |= Q(ip_address__startswith=q)
    qs = scoping.filter_by_site(Host.objects.exclude(status="rejected"), request.user).filter(match)
    return [{"kind": "host", "id": str(h.id), "title": h.hostname,
             "subtitle": " · ".join(x for x in (h.os, h.ip_address or "") if x)}
            for h in qs.order_by("hostname")[:LIMIT]]


def _containers(request, q):
    from .models import DockerContainer
    qs = (scoping.filter_by_site(DockerContainer.objects.exclude(host__status="rejected"),
                                 request.user, path="host__")
          .filter(Q(name__icontains=q) | Q(image__icontains=q)).select_related("host"))
    return [{"kind": "container", "id": c.name, "host_id": str(c.host_id), "title": c.name,
             "subtitle": f"{c.image} on {c.host.hostname}"}
            for c in qs.order_by("name")[:LIMIT]]


def _stacks(request, q):
    from apps.stacks.models import ManagedStack
    qs = (scoping.filter_by_site(ManagedStack.objects.all(), request.user, path="host__")
          .filter(name__icontains=q).select_related("host"))
    return [{"kind": "stack", "id": str(s.id), "host_id": str(s.host_id), "title": s.name,
             "subtitle": f"on {s.host.hostname} · r{s.revision}"}
            for s in qs.order_by("name")[:LIMIT]]


def _apps(request, q):
    from apps.software.models import SoftwareItem
    qs = (scoping.filter_by_site(SoftwareItem.objects.exclude(host__status="rejected"),
                                 request.user, path="host__")
          .filter(Q(name__icontains=q) | Q(publisher__icontains=q))
          .values("name").annotate(hosts=Count("host", distinct=True)).order_by("-hosts", "name"))
    return [{"kind": "app", "id": row["name"], "title": row["name"],
             "subtitle": "on " + _plural(row["hosts"], "host")}
            for row in qs[:LIMIT]]


def _cves(request, q):
    from apps.vulns.models import VulnFinding
    qs = (scoping.filter_by_site(VulnFinding.objects.filter(state="open").exclude(cve_id=""),
                                 request.user, path="host__")
          .filter(Q(cve_id__icontains=q) | Q(title__icontains=q)))
    ids = list(qs.values("cve_id").annotate(hosts=Count("host", distinct=True))
               .order_by("-hosts", "cve_id")[:LIMIT])
    out = []
    for row in ids:
        findings = list(qs.filter(cve_id=row["cve_id"]).values("severity", "title")[:50])
        worst = min((f["severity"] for f in findings), key=lambda s: _SEVERITY_RANK.get(s, 9))
        title = next((f["title"] for f in findings if f["title"]), "")
        out.append({"kind": "cve", "id": row["cve_id"], "title": row["cve_id"],
                    "subtitle": " · ".join(x for x in (worst, _plural(row["hosts"], "host"), title) if x)})
    return out


def _tasks(request, q):
    from apps.tasks.models import TaskDefinition
    qs = (TaskDefinition.objects.filter(archived_at__isnull=True)
          .filter(Q(owner=request.user) | Q(visibility=TaskDefinition.Visibility.COMMUNITY))
          .filter(Q(name__icontains=q) | Q(description__icontains=q)))
    return [{"kind": "task", "id": str(d.id), "title": d.name, "subtitle": (d.description or "")[:80]}
            for d in qs.order_by("name")[:LIMIT]]


def _playbooks(request, q):
    from apps.playbooks.models import Playbook
    qs = (scoping.filter_by_site(Playbook.objects.filter(archived_at__isnull=True), request.user,
                                 cascade_global=True)
          .filter(Q(name__icontains=q) | Q(description__icontains=q)))
    return [{"kind": "playbook", "id": str(b.id), "title": b.name,
             "subtitle": (b.description or "")[:80]}
            for b in qs.order_by("name")[:LIMIT]]


def _policies(request, q):
    from apps.policies.models import UpdatePolicy
    qs = UpdatePolicy.objects.filter(name__icontains=q)
    return [{"kind": "policy", "id": str(p.id), "title": p.name,
             "subtitle": "enabled" if p.enabled else "disabled"}
            for p in qs.order_by("name")[:LIMIT]]


def _alerts(request, q):
    from apps.alerts.models import Alert
    qs = (scoping.filter_by_site(Alert.objects.filter(state=Alert.State.FIRING), request.user,
                                 path="host__")
          .filter(Q(message__icontains=q) | Q(host__hostname__icontains=q)).select_related("host"))
    return [{"kind": "alert", "id": str(a.id), "title": (a.message or "")[:100],
             "subtitle": f"{a.severity} on {a.host.hostname}"}
            for a in qs.order_by("-fired_at")[:LIMIT]]


#: (kind, label, finder, admin only) — in the order the page shows them.
_KINDS = (
    ("host", "Hosts", _hosts, False),
    ("container", "Containers", _containers, False),
    ("stack", "Stacks", _stacks, True),
    ("app", "Apps", _apps, False),
    ("cve", "Vulnerabilities", _cves, False),
    ("task", "Tasks", _tasks, False),
    ("playbook", "Playbooks", _playbooks, False),
    ("policy", "Update policies", _policies, True),
    ("alert", "Firing alerts", _alerts, False),
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search(request):
    """One box over everything the user may already see (QA-25)."""
    q = request.query_params.get("q", "").strip()[:MAX_QUERY]
    if len(q) < 2:
        return Response({"query": q, "groups": []})
    admin = IsAdmin().has_permission(request, None)
    groups = []
    for kind, label, find, admin_only in _KINDS:
        if admin_only and not admin:
            continue
        results = find(request, q)
        if results:
            groups.append({"kind": kind, "label": label, "results": results})
    return Response({"query": q, "groups": groups})
