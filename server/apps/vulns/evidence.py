"""Evidence for findings, and the confidence it adds up to (M9).

``confidence`` reads the evidence kinds on a finding:

* **urgent** — installed (a package, registry entry or missing update) *and*
  live (a running process or a listening port): the vulnerable code is
  running right now.
* **confirmed** — installed, per the host's own inventory or Windows Update.
* **file** — only a file on disk matched. Labelled as file evidence: it may
  be a leftover nobody runs.
* **reported** — a scanner said so and nothing else has been checked.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils.timezone import now

from .models import FindingEvidence, VulnFinding

K = FindingEvidence.Kind
INSTALLED = {K.PACKAGE, K.REGISTRY, K.MISSING_UPDATE, K.OUTDATED_APP}
LIVE = {K.PROCESS, K.PORT}

#: How fresh a hunt result must be to count as "running now".
RUNTIME_WINDOW = timedelta(days=7)


def add_evidence(finding: VulnFinding, kind: str, key: str, summary: str = "",
                 detail: dict | None = None) -> FindingEvidence:
    row, _ = FindingEvidence.objects.update_or_create(
        finding=finding, kind=kind, key=str(key)[:300],
        defaults={"summary": summary[:300], "detail": detail or {}})
    return row


def confidence_for(kinds) -> str:
    kinds = set(kinds)
    if kinds & INSTALLED and kinds & LIVE:
        return "urgent"
    if kinds & INSTALLED:
        return "confirmed"
    if K.FILE in kinds:
        return "file"
    return "reported"


def confidence(finding: VulnFinding) -> str:
    return confidence_for(e.kind for e in finding.evidence.all())


def _names(finding: VulnFinding) -> set[str]:
    """What a process of this package would be called — the package name and
    the affected file's basename, lowercased, ignoring anything too short to
    mean anything."""
    names = set()
    pkg = (finding.package_name or "").lower()
    if pkg:
        names.add(pkg.split(":")[-1])
    if finding.affected_path:
        names.add(finding.affected_path.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return {n for n in names if len(n) >= 3}


def attach_runtime_evidence(host, findings=None) -> int:
    """Cite recent hunt_process / hunt_port matches on *host* that name a
    finding's package. Returns how many rows were added or refreshed.

    Matching is by name: a process whose name or command line contains the
    package name (or the affected file's name). It is evidence, not proof —
    which is why it only ever raises a finding that already has an installed
    source to urgent, and never creates one.
    """
    from apps.tasks.models import HuntMatch

    if findings is None:
        findings = list(VulnFinding.objects.filter(host=host, state=VulnFinding.State.OPEN))
    matches = list(HuntMatch.objects.filter(
        host=host, evidence_type__in=("process", "port"),
        created_at__gte=now() - RUNTIME_WINDOW).order_by("-created_at")[:2000])
    added = 0
    for finding in findings:
        names = _names(finding)
        if not names:
            continue
        for m in matches:
            data = m.data or {}
            if m.evidence_type == "process":
                hay = f"{data.get('name') or ''} {data.get('cmdline') or ''}".lower()
                if any(n in hay for n in names):
                    add_evidence(finding, K.PROCESS, f"{data.get('pid')}:{data.get('name')}",
                                 f"running as {data.get('name')} (pid {data.get('pid')})",
                                 {"name": data.get("name"), "pid": data.get("pid"),
                                  "user": data.get("user")})
                    added += 1
            else:
                proc = str(data.get("process") or "").lower()
                if proc and any(n in proc for n in names):
                    add_evidence(finding, K.PORT, f"{data.get('protocol')}/{data.get('port')}",
                                 f"listening on {data.get('protocol')}/{data.get('port')} as {proc}",
                                 {"port": data.get("port"), "protocol": data.get("protocol"),
                                  "address": data.get("address"), "process": proc})
                    added += 1
    return added
