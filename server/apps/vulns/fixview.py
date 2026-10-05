"""Remediation by fix (M9): findings grouped across the fleet by the one
action that resolves them — "Upgrade openssl on 14 hosts: fixes 37 CVEs".

Each group answers four questions: **what** (the package or update, the
version installed, the path), **where** (the hosts, with each host's evidence
and confidence), **why** (worst severity, CVSS, EPSS, KEV, whether it is
running) and **fix** (the fixed version and the task that applies it, or
"no fix available" with the vendor's status).
"""

from __future__ import annotations

import yaml
from django.db.models import Q

from .evidence import confidence_for
from .models import EpssScore, KevEntry, VulnFinding
from .scoring import SEVERITY_RANK

_LINUX_SOURCES = {"dpkg", "rpm", "apk", "pacman"}


def _fix_for(finding: VulnFinding, detections: dict | None = None) -> dict:
    """How this group is fixed: a deterministic task, a matching detection
    task (M10), or nothing."""
    key = finding.plugin_id_or_oid
    if key.startswith("det:"):
        return {"kind": "detection", "label": f"Run the detection task {finding.title}",
                "action": "", "params": {}, "definition_id": key.split(":")[1]}
    if key.startswith("kb:"):
        return {"kind": "kb", "label": f"Install {finding.fixed_version}",
                "action": "windows_update_install", "params": {"include_kb": finding.fixed_version}}
    if key.startswith("app:"):
        source = key.split(":")[1]
        return {"kind": "upgrade", "label": f"Upgrade {finding.package_name} to {finding.fixed_version}",
                "action": "app_upgrade", "params": {"app": finding.package_name, "source": source}}
    if finding.package_name and finding.fixed_version and not finding.affected_path:
        params = {"app": finding.package_name}
        source = (finding.advisory or {}).get("source") if isinstance(finding.advisory, dict) else None
        if source in _LINUX_SOURCES:
            params["source"] = source
        return {"kind": "upgrade",
                "label": f"Upgrade {finding.package_name} to {finding.fixed_version} or later",
                "action": "app_upgrade", "params": params}
    match = (detections or {}).get((finding.cve_id or "").upper())
    if match is not None:
        return {"kind": "detection", "label": f"Run the detection task {match.name}",
                "action": "", "params": {}, "definition_id": str(match.id)}
    return {"kind": "none", "label": "No fix available" if not finding.fixed_version
            else f"Fixed in {finding.fixed_version} — update the file by hand or with a task",
            "action": "", "params": {}}


def fix_groups(user, *, q: str = "") -> list[dict]:
    from vigil import scoping

    qs = scoping.filter_by_site(
        VulnFinding.objects.filter(state=VulnFinding.State.OPEN).exclude(host__status="rejected"),
        user, path="host__").select_related("host").prefetch_related("evidence")
    if q:
        qs = qs.filter(Q(package_name__icontains=q) | Q(cve_id__icontains=q) | Q(title__icontains=q))
    groups: dict[str, list[VulnFinding]] = {}
    for f in qs[:20000]:
        groups.setdefault(f.fix_key or f"{f.scanner}:{f.plugin_id_or_oid}", []).append(f)

    cves = {f.cve_id.upper() for fs in groups.values() for f in fs if f.cve_id}
    kev = set(KevEntry.objects.filter(cve_id__in=cves).values_list("cve_id", flat=True))
    epss = dict(EpssScore.objects.filter(cve_id__in=cves).values_list("cve_id", "epss"))

    detections = detection_tasks_by_cve(user)
    out = []
    for key, members in groups.items():
        lead = max(members, key=lambda f: SEVERITY_RANK.get(f.severity, -1))
        hosts: dict = {}
        for f in members:
            h = hosts.setdefault(f.host_id, {"host_id": str(f.host_id), "hostname": f.host.hostname,
                                             "installed_version": f.installed_version,
                                             "evidence": set(), "evidence_text": []})
            for e in f.evidence.all():
                if e.kind not in h["evidence"]:
                    h["evidence"].add(e.kind)
                    h["evidence_text"].append(e.summary)
        for h in hosts.values():
            h["confidence"] = confidence_for(h["evidence"])
            h["evidence"] = sorted(h["evidence"])
        group_cves = sorted({f.cve_id.upper() for f in members if f.cve_id})
        dues = [f.due_date for f in members if f.due_date]
        scores = [f.cvss_score for f in members if f.cvss_score is not None]
        fix = _fix_for(lead, detections)
        if fix["kind"] == "none":
            for cve in group_cves:
                if cve in detections:
                    fix = _fix_for(next(f for f in members if f.cve_id.upper() == cve), detections)
                    break
        out.append({
            "fix_key": key,
            "title": lead.title,
            "package": lead.package_name or lead.title,
            "installed_version": lead.installed_version,
            "fixed_version": lead.fixed_version,
            "affected_path": lead.affected_path,
            "vendor_status": lead.vendor_status,
            "severity": lead.severity,
            "cves": group_cves,
            "finding_count": len(members),
            "host_count": len(hosts),
            "hosts": sorted(hosts.values(), key=lambda h: h["hostname"]),
            "due_date": min(dues).isoformat() if dues else None,
            "cvss": max(scores) if scores else None,
            "epss": max((epss[c] for c in group_cves if c in epss), default=None),
            "kev": any(c in kev for c in group_cves),
            "running": any(h["confidence"] == "urgent" for h in hosts.values()),
            "scanners": sorted({f.scanner for f in members}),
            "fix": {k: v for k, v in fix.items() if k not in ("params", "action")},
        })
    out.sort(key=lambda g: (-SEVERITY_RANK.get(g["severity"], 0), not g["kev"], not g["running"],
                            -g["host_count"], g["package"]))
    return out


def detection_tasks_by_cve(user) -> dict:
    """CVE → a detection task (relevant: + severity) that names it, from the
    tasks this user can see. A matching detection task is the fix for a group
    with no package fix (M10)."""
    from apps.tasks.models import TaskDefinition

    out: dict = {}
    qs = TaskDefinition.objects.filter(archived_at__isnull=True).filter(
        Q(owner=user) | Q(owner__isnull=True) | Q(visibility=TaskDefinition.Visibility.COMMUNITY))
    for definition in qs.order_by("-updated_at"):
        spec = definition.parsed_spec or {}
        if not spec.get("severity") or not spec.get("relevant"):
            continue
        for cve in spec.get("cves") or []:
            out.setdefault(str(cve).upper(), definition)
    return out


def fix_definition(user, fix_key: str):
    """The task that applies one group's fix, created on first use and reused
    after. None when the group has no deterministic fix."""
    from apps.tasks.models import TaskDefinition
    from apps.tasks.spec import parse_and_validate

    lead = (VulnFinding.objects.filter(fix_key=fix_key, state=VulnFinding.State.OPEN)
            .order_by("-cvss_score").first())
    if lead is None:
        return None, []
    members = list(VulnFinding.objects.filter(fix_key=fix_key, state=VulnFinding.State.OPEN))
    detections = detection_tasks_by_cve(user)
    fix = _fix_for(lead, detections)
    if fix["kind"] == "none":
        fix = next((_fix_for(f, detections) for f in members
                    if (f.cve_id or "").upper() in detections), fix)
    if fix["kind"] == "none":
        return None, []
    host_ids = sorted({str(f.host_id) for f in members})
    if fix["kind"] == "detection":
        from apps.tasks.models import TaskDefinition
        return TaskDefinition.objects.filter(pk=fix["definition_id"]).first(), host_ids
    name = f"Fix: {fix['label']}"[:120]
    doc = {"name": name,
           "description": f"Generated from the Vulnerabilities page to fix {lead.package_name}.",
           "risk": "standard",
           "actions": [{"id": "fix", "type": fix["action"], "params": fix["params"]}]}
    source = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    spec = parse_and_validate(source)
    definition = (TaskDefinition.objects.filter(name=name, owner=user, archived_at__isnull=True).first()
                  or TaskDefinition(owner=user, visibility=TaskDefinition.Visibility.PRIVATE))
    if lead.scanner == "anvil":
        # Made from a vendor's findings: tagged so the library says so.
        definition.content_source = TaskDefinition.ContentSource.VENDOR
    definition.yaml_source, definition.parsed_spec = source, spec
    definition.name, definition.description = spec["name"], spec["description"]
    definition.relevance, definition.risk_level = spec["relevance"], spec["risk"]
    definition.save()
    return definition, host_ids
