"""Vigil's own matcher: the Apps inventory against the local OSV copy (M9).

For each Linux host, the packages its own manager reports are compared with
the OSV advisories for that host's distribution release. A package inside an
affected range becomes a finding (scanner ``vigil``) that cites the package
record as evidence, carries the advisory's text, score and fixed version,
and is fixed again when a later inventory shows the package past it.

The distribution comes from the host's os-release (HostInventory): Ubuntu
24.04 reads ``Ubuntu:24.04`` and ``Ubuntu:24.04:LTS``, Debian 12 reads
``Debian:12``, and so on. A host whose release has no OSV ecosystem —
Arch, SUSE, RHEL itself — is simply not matched.
"""

from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils.timezone import now

from apps.software.models import SoftwareItem

from . import versions
from .evidence import add_evidence
from .models import FindingEvidence, OsvAffected, VulnFinding

logger = logging.getLogger(__name__)

#: plugin_id_or_oid prefix for package findings. Other Vigil sources (missing
#: updates, outdated apps) use their own, so each reconciles only its own rows.
PKG_PREFIX = "pkg:"
KB_PREFIX = "kb:"
APP_PREFIX = "app:"

#: Windows Update classifications whose missing updates are vulnerabilities.
SECURITY_CLASSIFICATIONS = {"Security Updates", "Critical Updates"}
_WU_SEVERITY = {"critical": "critical", "important": "high", "moderate": "medium", "low": "low"}

_RELEASE = re.compile(r"(\d+(?:\.\d+)?)")


def host_ecosystems(host) -> tuple[list[str], str]:
    """(OSV ecosystems, version scheme) for *host*, or ([], "") when none."""
    inv = getattr(host, "inventory", None)
    if inv is None:
        return [], ""
    name = (inv.os_name or "").lower()
    version = (inv.os_version or "").strip()
    major = version.split(".")[0] if version else ""
    if "ubuntu" in name and version:
        short = ".".join(version.split(".")[:2])
        return [f"Ubuntu:{short}", f"Ubuntu:{short}:LTS", f"Ubuntu:Pro:{short}:LTS"], "deb"
    if "debian" in name and major:
        return [f"Debian:{major}"], "deb"
    if "alpine" in name and version:
        short = ".".join(version.split(".")[:2])
        return [f"Alpine:v{short}"], "generic"
    if "rocky" in name and major:
        return [f"Rocky Linux:{major}"], "rpm"
    if "alma" in name and major:
        return [f"AlmaLinux:{major}"], "rpm"
    return [], ""


def _affected(version: str, row: OsvAffected, scheme: str) -> bool:
    """True when *version* falls inside one of *row*'s ranges or its list."""
    if version in (row.versions or []):
        return True
    for rng in row.ranges or []:
        if rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
            continue
        introduced = None
        for event in rng.get("events") or []:
            if "introduced" in event:
                introduced = str(event["introduced"])
            elif introduced is not None and ("fixed" in event or "last_affected" in event):
                after = introduced == "0" or versions.compare(version, introduced, scheme) >= 0
                if "fixed" in event:
                    inside = versions.compare(version, str(event["fixed"]), scheme) < 0
                else:
                    inside = versions.compare(version, str(event["last_affected"]), scheme) <= 0
                if after and inside:
                    return True
                introduced = None
        if introduced is not None and (
                introduced == "0" or versions.compare(version, introduced, scheme) >= 0):
            return True   # introduced and never fixed
    return False


def _reconcile(host, prefix: str, seen: set[str]) -> int:
    stale = (VulnFinding.objects.filter(host=host, scanner="vigil", state=VulnFinding.State.OPEN,
                                        plugin_id_or_oid__startswith=prefix)
             .exclude(plugin_id_or_oid__in=seen))
    return stale.update(state=VulnFinding.State.FIXED, resolved_at=now())


def match_windows_updates(host) -> dict:
    """A missing Windows security update *is* the vulnerability: one finding
    per missing security or critical KB, rated by Windows Update's own
    severity, fixed by installing that KB."""
    from apps.software.models import PendingUpdate

    seen: set[str] = set()
    with transaction.atomic():
        for row in PendingUpdate.objects.filter(host=host, kind=PendingUpdate.Kind.WINDOWS):
            if row.classification not in SECURITY_CLASSIFICATIONS and not row.severity:
                continue
            key = f"{KB_PREFIX}{row.key}"[:128]
            seen.add(key)
            finding, _ = VulnFinding.objects.update_or_create(
                host=host, scanner="vigil", plugin_id_or_oid=key,
                defaults={
                    "title": (row.title or row.key)[:255],
                    "severity": _WU_SEVERITY.get(row.severity, VulnFinding.Severity.MEDIUM),
                    "package_name": row.key[:255], "fixed_version": row.key[:80],
                    "vendor_status": "fixed",
                    "advisory": {"kb": row.key, "classification": row.classification},
                    "state": VulnFinding.State.OPEN, "resolved_at": None,
                })
            add_evidence(finding, FindingEvidence.Kind.MISSING_UPDATE, row.key,
                         f"{row.key} not installed ({row.classification or 'Windows Update'})",
                         {"classification": row.classification, "severity": row.severity})
        fixed = _reconcile(host, KB_PREFIX, seen)
    return {"open": len(seen), "fixed": fixed}


def match_outdated_apps(host) -> dict:
    """Third-party Windows apps, v1: an app winget or Chocolatey says is
    outdated is treated as vulnerable, medium, fixed by upgrading it. A proxy
    — there is no advisory feed behind it — and labelled so."""
    seen: set[str] = set()
    with transaction.atomic():
        for item in SoftwareItem.objects.filter(host=host, source__in=("winget", "chocolatey")):
            if not item.outdated:
                continue
            key = f"{APP_PREFIX}{item.source}:{item.package_id}"[:128]
            seen.add(key)
            finding, _ = VulnFinding.objects.update_or_create(
                host=host, scanner="vigil", plugin_id_or_oid=key,
                defaults={
                    "title": f"{item.name} is out of date"[:255],
                    "severity": VulnFinding.Severity.MEDIUM,
                    "package_name": item.package_id[:255],
                    "installed_version": item.version[:80],
                    "fixed_version": item.latest_version[:80],
                    "vendor_status": "fixed",
                    "description": (f"{item.source} offers {item.name} {item.latest_version}; this "
                                    f"host has {item.version}. Vigil treats an outdated app as "
                                    f"vulnerable until it is current — there is no advisory feed "
                                    f"behind this finding."),
                    "advisory": {"proxy": "outdated", "source": item.source},
                    "state": VulnFinding.State.OPEN, "resolved_at": None,
                })
            add_evidence(finding, FindingEvidence.Kind.OUTDATED_APP, f"{item.source}:{item.package_id}",
                         f"{item.version} installed, {item.latest_version} available ({item.source})",
                         {"source": item.source, "version": item.version,
                          "latest": item.latest_version})
        fixed = _reconcile(host, APP_PREFIX, seen)
    return {"open": len(seen), "fixed": fixed}


def match_host(host) -> dict:
    """Every Vigil source for one host; returns {"open": n, "fixed": n}."""
    total = {"open": 0, "fixed": 0}
    for part in (_match_packages(host), match_windows_updates(host), match_outdated_apps(host)):
        total["open"] += part["open"]
        total["fixed"] += part["fixed"]
    from .scoring import recompute_summary
    recompute_summary(host)
    return total


def _match_packages(host) -> dict:
    """The inventory against OSV; returns {"open": n, "fixed": n}."""
    ecosystems, scheme = host_ecosystems(host)
    if not ecosystems:
        return {"open": 0, "fixed": 0}
    items = {i.package_id: i for i in SoftwareItem.objects.filter(
        host=host, source__in=("dpkg", "rpm", "apk"))}
    rows = (OsvAffected.objects.filter(ecosystem__in=ecosystems, package__in=list(items))
            .select_related("advisory"))
    seen: set[str] = set()
    with transaction.atomic():
        for row in rows:
            adv = row.advisory
            item = items[row.package]
            if adv.withdrawn or not _affected(item.version, row, scheme):
                continue
            for cve in adv.cve_ids or [adv.id]:
                key = f"{PKG_PREFIX}{item.package_id}:{cve}"[:128]
                if key in seen:
                    continue
                seen.add(key)
                finding, _ = VulnFinding.objects.update_or_create(
                    host=host, scanner="vigil", plugin_id_or_oid=key,
                    defaults={
                        "cve_id": cve if cve.startswith("CVE-") else "",
                        "title": (adv.summary or f"{cve} in {item.package_id}")[:255],
                        "severity": adv.severity or VulnFinding.Severity.MEDIUM,
                        "package_name": item.package_id[:255],
                        "installed_version": item.version[:80],
                        "fixed_version": row.fixed[:80],
                        "description": adv.details,
                        "cvss_score": adv.cvss_score,
                        "cvss_vector": adv.cvss_vector,
                        "references": adv.references,
                        "vendor_status": "fixed" if row.fixed else "affected",
                        "advisory": {"osv_id": adv.id, "ecosystem": row.ecosystem,
                                     "source": item.source},
                        "state": VulnFinding.State.OPEN,
                        "resolved_at": None,
                    })
                add_evidence(finding, FindingEvidence.Kind.PACKAGE,
                             f"{item.source}:{item.package_id}",
                             f"{item.package_id} {item.version} installed ({item.source})",
                             {"source": item.source, "version": item.version})
        fixed = _reconcile(host, PKG_PREFIX, seen)
    from .evidence import attach_runtime_evidence
    attach_runtime_evidence(host, VulnFinding.objects.filter(
        host=host, scanner="vigil", state=VulnFinding.State.OPEN, plugin_id_or_oid__in=seen))
    return {"open": len(seen), "fixed": fixed}


def match_all() -> int:
    """Match every Linux host that has reported its inventory."""
    from apps.hosts.models import Host

    n = 0
    for host in Host.objects.exclude(status=Host.Status.REJECTED).filter(
            software_snapshot__isnull=False).select_related("inventory"):
        try:
            match_host(host)
            n += 1
        except Exception:  # noqa: BLE001 — one bad host must not stop the fleet
            logger.exception("vulnerability match failed for %s", host.hostname)
    return n
