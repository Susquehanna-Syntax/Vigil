"""Detection tasks become findings (M10).

A detection task is an ordinary task with a ``relevant:`` block and a
``severity:`` — the relevant: probes are the detection, the steps are the
fix. When one reports from a host:

* **not applicable** — nothing matched; any finding it raised there before
  is fixed (it is no longer detected);
* **completed** — it matched *and* its fix ran: the finding is recorded and
  closed in one go, so the history shows it was found and fixed;
* **failed** — it matched and the fix did not finish: the finding stays open.

The probes' matches (files, processes, packages, ports) are the finding's
evidence, and a ``boost:`` match raises its confidence.
"""

from __future__ import annotations

from django.utils.timezone import now

from .evidence import add_evidence
from .models import FindingEvidence, VulnFinding

_EVIDENCE_KIND = {
    "file": FindingEvidence.Kind.FILE,
    "process": FindingEvidence.Kind.PROCESS,
    "port": FindingEvidence.Kind.PORT,
    "package": FindingEvidence.Kind.PACKAGE,
    "registry": FindingEvidence.Kind.REGISTRY,
}


def _detection_spec(task):
    run = getattr(task, "run", None)
    definition = getattr(run, "definition", None) if run is not None else None
    spec = getattr(definition, "parsed_spec", None) or {}
    if not spec.get("severity") or not (task.params or {}).get("relevant"):
        return None, None
    return definition, spec


def _confidence(task) -> str:
    for step in (task.result_data or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("id") == "detection":
            return str((step.get("result") or {}).get("confidence") or "")
    return ""


def record_detection(task) -> int:
    """Record what a detection task found on its host; returns rows touched."""
    from apps.tasks.models import HuntMatch, Task

    from .scoring import recompute_summary

    definition, spec = _detection_spec(task)
    if definition is None:
        return 0
    base = f"det:{definition.id}"
    host = task.host
    if task.state == Task.State.NOT_APPLICABLE:
        n = VulnFinding.objects.filter(host=host, plugin_id_or_oid__startswith=base,
                                       state=VulnFinding.State.OPEN).update(
            state=VulnFinding.State.FIXED, resolved_at=now())
        if n:
            recompute_summary(host)
        return n

    fixed = task.state == Task.State.COMPLETED
    confidence = _confidence(task)
    matches = list(HuntMatch.objects.filter(task=task))
    touched = 0
    for cve in spec.get("cves") or [""]:
        finding, _ = VulnFinding.objects.update_or_create(
            host=host, scanner="detection",
            plugin_id_or_oid=f"{base}:{cve or 'match'}"[:128],
            defaults={
                "cve_id": cve.upper(),
                "title": definition.name[:255],
                "severity": spec["severity"],
                "description": spec.get("description") or "",
                "references": list(spec.get("references") or [])[:50],
                "vendor_status": "fixed",
                "advisory": {"definition_id": str(definition.id), "task_id": str(task.id),
                             "confidence": confidence},
                "state": VulnFinding.State.FIXED if fixed else VulnFinding.State.OPEN,
                "resolved_at": now() if fixed else None,
            })
        for m in matches:
            kind = _EVIDENCE_KIND.get(m.evidence_type)
            if kind is None:
                continue
            data = m.data or {}
            key = str(data.get("path") or data.get("name") or data.get("key")
                      or f"{data.get('protocol')}/{data.get('port')}")
            prefix = "boost: " if m.step_id.startswith("boost-") else ""
            add_evidence(finding, kind, key, f"{prefix}{m.evidence_type} {key}"[:300],
                         {"step": m.step_id, **{k: v for k, v in data.items()
                                                if isinstance(v, (str, int, float, bool))}})
        touched += 1
    recompute_summary(host)
    return touched
