"""Which hosts a policy covers, and what would change on each.

Drift is computed here, on the server, from the inventory each host already
sent — not with a ``relevant:`` probe. The only package probe (hunt_package)
reads Linux package managers and nothing else, so it cannot see winget,
Chocolatey, snap or the registry; the inventory can. A policy run then
dispatches only to hosts with something to change.
"""

from __future__ import annotations

from django.db.models import Q

from apps.software.models import SoftwareItem

from .models import AppRule

INSTALL, UPGRADE, PIN, UNINSTALL = "install", "upgrade", "pin", "uninstall"


def policy_hosts(policy):
    """Managed hosts the policy covers — the same base set an automation uses
    (no pending, rejected or monitor-mode hosts), narrowed by tag and site."""
    from apps.hosts.models import Host

    qs = (Host.objects.exclude(status=Host.Status.PENDING)
          .exclude(status=Host.Status.REJECTED)
          .exclude(mode=Host.Mode.MONITOR))
    tags = [str(t).lower() for t in (policy.target_tags or []) if str(t).strip()]
    if tags:
        qs = qs.filter(tag_rows__key__in=tags)
    if policy.site_id:
        from vigil.scoping import _sites_models
        sites = _sites_models()
        site = sites.Site.objects.filter(pk=policy.site_id).first() if sites else None
        if site is None:
            return qs.none()
        if site.is_global:
            # No assignment row means the global site.
            qs = qs.filter(Q(site_assignment__isnull=True)
                           | Q(site_assignment__site__is_global=True))
        else:
            qs = qs.filter(site_assignment__site_id=site.pk)
    return qs.distinct().order_by("hostname")


def _matches(rule, item) -> bool:
    return item.package_id == rule.app and (not rule.source or item.source == rule.source)


def rule_change(rule, items) -> dict | None:
    """The change *rule* needs on a host whose inventory is *items*, or None
    when the host already complies.

    ========  ===================  ==========================================
    state     no matching row      matching row
    ========  ===================  ==========================================
    present   install              compliant
    latest    install              outdated → upgrade to latest, else compliant
    pinned    install (version)    other version → pin, else compliant
    absent    compliant            uninstall
    ========  ===================  ==========================================
    """
    rows = sorted((i for i in items if _matches(rule, i)),
                  key=lambda i: (i.source, i.version))
    base = {"app": rule.app, "source": rule.source, "state": rule.state}
    S = AppRule.State
    if not rows:
        if rule.state == S.ABSENT:
            return None
        return {**base, "action": INSTALL, "have": "",
                "want": rule.version if rule.state == S.PINNED else ""}
    row = rows[0]
    if rule.state == S.PRESENT:
        return None
    if rule.state == S.ABSENT:
        return {**base, "action": UNINSTALL, "have": row.version, "want": "",
                "source": row.source}
    if rule.state == S.LATEST:
        stale = next((r for r in rows if r.outdated), None)
        if stale is None:
            return None
        return {**base, "action": UPGRADE, "have": stale.version,
                "want": stale.latest_version, "source": stale.source}
    # pinned
    wrong = next((r for r in rows if r.version != rule.version), None)
    if wrong is None:
        return None
    return {**base, "action": PIN, "have": wrong.version, "want": rule.version,
            "source": wrong.source}


def policy_drift(policy) -> dict:
    """``{"hosts": [{host_id, hostname, changes}], "compliant": n,
    "unknown": [{host_id, hostname}]}``.

    A host that has never reported its inventory is *unknown*, not drifted:
    installing everything on a host because we have not heard from it yet
    would be the wrong guess.
    """
    hosts = list(policy_hosts(policy).select_related("software_snapshot"))
    rules = list(policy.app_rules.all())
    apps = {r.app for r in rules}
    by_host: dict = {}
    if apps and hosts:
        for item in SoftwareItem.objects.filter(
                host_id__in=[h.pk for h in hosts], package_id__in=apps):
            by_host.setdefault(item.host_id, []).append(item)

    drifted, unknown, compliant = [], [], 0
    for host in hosts:
        if not _has_snapshot(host):
            unknown.append({"host_id": str(host.pk), "hostname": host.hostname})
            continue
        items = by_host.get(host.pk, [])
        changes = [c for c in (rule_change(r, items) for r in rules) if c]
        changes.extend(_extra_changes(policy, host))
        if changes:
            drifted.append({"host_id": str(host.pk), "hostname": host.hostname,
                            "changes": changes})
        else:
            compliant += 1
    return {"hosts": drifted, "compliant": compliant, "unknown": unknown}


def _has_snapshot(host) -> bool:
    try:
        return host.software_snapshot is not None
    except Exception:  # noqa: BLE001 — RelatedObjectDoesNotExist
        return False


def _extra_changes(policy, host) -> list[dict]:
    """Changes beyond the app rules — pending OS updates once the Patching tab
    is on (phase 07). Empty until then."""
    return []
