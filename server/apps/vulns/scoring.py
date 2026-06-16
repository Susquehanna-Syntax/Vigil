"""Vulnerability score computation.

A single source of truth for the score that decorates every host and
the fleet headline. The formula is intentionally simple — users should
be able to read it off the tooltip and instantly know why their host
scored what it scored.

Weights (per finding):

    critical: 10        high: 3        medium: 1        low: 0.2

    score = 100 - round(10×crit + 3×high + 1×med + 0.2×low)

There's deliberately **no floor**. A host with 15 criticals lands at
`-50`; that number being negative is part of the message. The face
badge escalates with the score, so `-150` reads visually worse than
`-50` and both read worse than `0`.

When a host has the same CVE reported by multiple scanners we count it
once — see :func:`recompute_summary`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.hosts.models import Host
    from .models import VulnSummary


# Weights are tuned for "harsh on highs, lows still count, criticals are
# game over." Tweaking these changes the score for every host in the
# fleet at once — keep them in lockstep with the spec.
_WEIGHT_CRITICAL = 10
_WEIGHT_HIGH = 3
_WEIGHT_MEDIUM = 1
_WEIGHT_LOW = 0.2

# Numeric severity order. The TextChoices values sort alphabetically
# ("medium" > "critical"!) so anything that needs worst-first ordering
# must rank through this map, never through the raw strings.
SEVERITY_RANK = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}


def compute_score(critical: int, high: int, medium: int, low: int) -> int:
    """Return the integer score for the given severity counts.

    Score = 100 minus the weighted deduction, rounded to an integer.
    Result can be negative — that's intentional, see module docstring.
    """
    deduction = (
        _WEIGHT_CRITICAL * critical
        + _WEIGHT_HIGH * high
        + _WEIGHT_MEDIUM * medium
        + _WEIGHT_LOW * low
    )
    return 100 - int(round(deduction))


def recompute_summary(host: "Host") -> "VulnSummary":
    """Recount findings + recompute score for one host.

    Walks every ``OPEN`` :class:`VulnFinding` for ``host``, dedupes by
    CVE (or by scanner+plugin when no CVE is set), buckets by severity,
    writes the counts to :class:`VulnSummary`, and stores the resulting
    score + debt. ``info``-level findings don't affect the score but
    still get counted into the summary so the dashboard total matches
    the findings list.

    Idempotent — safe to call from any ingest path, or from a backfill
    script, or from a management command.
    """
    from .models import VulnFinding, VulnSummary

    counts: dict[str, int] = defaultdict(int)
    # Dedup by CVE so a host with the same CVE flagged by Nessus and
    # Trivy isn't double-penalized. When scanners disagree on severity,
    # the worst one wins — tracked via SEVERITY_RANK so the result
    # doesn't depend on row iteration order.
    best_by_cve: dict[str, str] = {}

    for finding in VulnFinding.objects.filter(host=host, state=VulnFinding.State.OPEN):
        if finding.cve_id:
            prev = best_by_cve.get(finding.cve_id)
            if prev is None or (
                SEVERITY_RANK.get(finding.severity, 0) > SEVERITY_RANK.get(prev, 0)
            ):
                best_by_cve[finding.cve_id] = finding.severity
            continue
        # No CVE → identity falls back to (scanner, plugin). Each
        # (host, scanner, plugin) is already unique via the model
        # constraint, so no extra dedup needed for this branch.
        counts[finding.severity] += 1

    for severity in best_by_cve.values():
        counts[severity] += 1

    summary, _ = VulnSummary.objects.get_or_create(host=host)
    summary.critical = counts.get(VulnFinding.Severity.CRITICAL, 0)
    summary.high = counts.get(VulnFinding.Severity.HIGH, 0)
    summary.medium = counts.get(VulnFinding.Severity.MEDIUM, 0)
    summary.low = counts.get(VulnFinding.Severity.LOW, 0)
    summary.info = counts.get(VulnFinding.Severity.INFO, 0)
    summary.score = compute_score(
        summary.critical, summary.high, summary.medium, summary.low,
    )
    summary.save(update_fields=[
        "critical", "high", "medium", "low", "info",
        "score", "synced_at",
    ])
    return summary
