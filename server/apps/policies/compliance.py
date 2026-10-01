"""Patch compliance: how much of the fleet is patched within its SLA.

An update is overdue when it has been pending longer than the remediation
policy allows for its severity — the same day tiers the vulnerability
views use (critical → critical_days, important → high_days, moderate →
medium_days, low → low_days). An update with no severity, which is every
Linux package, is held to the medium tier. A declined update is not
counted: declining it was a decision, and the report should not keep
re-litigating it.

The numbers are Free. The report a third party reads — per site, exported,
branded — is Business (apps_business/compliance).
"""

from __future__ import annotations

from django.utils.timezone import now

from apps.software.models import PendingUpdate, UpdateDecision

BUCKETS = (("0-7", 0, 7), ("8-30", 8, 30), ("31-90", 31, 90), ("90+", 91, None))
TRACKED_SEVERITIES = ("critical", "important")


def sla_days(policy, severity: str) -> int:
    return {
        "critical": policy.critical_days,
        "important": policy.high_days,
        "moderate": policy.medium_days,
        "low": policy.low_days,
    }.get((severity or "").lower(), policy.medium_days)


def _bucket(age: int) -> str:
    for name, low, high in BUCKETS:
        if age >= low and (high is None or age <= high):
            return name
    return BUCKETS[-1][0]


def _hosts(user=None):
    from apps.hosts.models import Host
    from vigil import scoping

    qs = (Host.objects.exclude(status=Host.Status.PENDING)
          .exclude(status=Host.Status.REJECTED).exclude(mode=Host.Mode.MONITOR))
    if user is not None:
        qs = scoping.filter_by_site(qs, user)
    return list(qs.order_by("hostname"))


def compliance(user=None, at=None) -> dict:
    """The fleet's patch compliance, scoped to what *user* may see.

    ``hosts`` is per-host detail; ``sites`` groups it (one Global row when
    sites are not in use).
    """
    from apps.vulns.models import RemediationPolicy

    at = at or now()
    sla = RemediationPolicy.get_active()
    hosts = _hosts(user)
    declined = {(d.kind, d.key) for d in UpdateDecision.objects.filter(
        decision=UpdateDecision.Decision.DECLINED)}
    per_host = {h.pk: {"host_id": str(h.pk), "hostname": h.hostname, "pending": 0,
                       "overdue": 0, "oldest_overdue_days": 0} for h in hosts}
    buckets = {sev: {name: 0 for name, _, _ in BUCKETS} for sev in TRACKED_SEVERITIES}
    for row in PendingUpdate.objects.filter(host_id__in=list(per_host)):
        if (row.kind, row.key) in declined:
            continue
        age = (at - row.first_seen).days
        entry = per_host[row.host_id]
        entry["pending"] += 1
        if age > sla_days(sla, row.severity):
            entry["overdue"] += 1
            entry["oldest_overdue_days"] = max(entry["oldest_overdue_days"], age)
        if row.severity in buckets:
            buckets[row.severity][_bucket(age)] += 1

    rows = list(per_host.values())
    patched = sum(1 for r in rows if r["overdue"] == 0)
    return {
        "generated_at": at.isoformat(),
        "hosts_total": len(rows),
        "hosts_patched": patched,
        "patched_pct": round(100 * patched / len(rows), 1) if rows else 100.0,
        "missing_by_age": buckets,
        "sla_days": {"critical": sla.critical_days, "important": sla.high_days,
                     "moderate": sla.medium_days, "low": sla.low_days},
        "hosts": rows,
        "sites": _by_site(hosts, per_host),
    }


def _by_site(hosts, per_host) -> list[dict]:
    from vigil.scoping import _sites_models

    sites = _sites_models()
    names: dict = {}
    if sites is not None:
        for a in sites.HostSiteAssignment.objects.filter(
                host_id__in=[h.pk for h in hosts]).select_related("site"):
            names[a.host_id] = "Global" if a.site.is_global else a.site.name
    grouped: dict = {}
    for host in hosts:
        site = names.get(host.pk, "Global")
        g = grouped.setdefault(site, {"site": site, "hosts": 0, "overdue_hosts": 0})
        g["hosts"] += 1
        if per_host[host.pk]["overdue"]:
            g["overdue_hosts"] += 1
    out = sorted(grouped.values(), key=lambda g: (g["site"] != "Global", g["site"]))
    for g in out:
        g["pass"] = g["overdue_hosts"] == 0
    return out


def summary(user=None) -> dict:
    """The Free numbers: everything but the per-host and per-site detail."""
    full = compliance(user)
    return {k: v for k, v in full.items() if k not in ("hosts", "sites")}
