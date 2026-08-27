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

The score that is stored on ``VulnSummary`` is the *escalated* one: each
finding's base weight is multiplied by its distance from its due date
(see :mod:`apps.vulns.remediation`). The un-escalated weighted count —
:func:`compute_score` — is kept as the baseline and is what a score would
be if every due date were far enough away to matter not.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.hosts.models import Host
    from .models import VulnFinding, VulnSummary

from .remediation import escalation_multiplier

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


def base_weight(severity: str) -> float:
    """The un-escalated weight of one finding at a given severity."""
    return {
        "critical": _WEIGHT_CRITICAL,
        "high": _WEIGHT_HIGH,
        "medium": _WEIGHT_MEDIUM,
        "low": _WEIGHT_LOW,
        "info": 0.0,
    }.get(severity, 0.0)


def compute_escalated_score(deduction: float) -> int:
    """100 minus a per-finding escalated deduction, no floor.

    ``deduction`` is the sum of ``base_weight × escalation_multiplier`` over
    the host's deduped open findings, accumulated by :func:`recompute_summary`.
    Rounding happens once, here, not per finding — per-finding rounding would
    drift the total.
    """
    return 100 - int(round(deduction))


def _finding_deduction(
    finding: "VulnFinding",
    on_date: date | None = None,
) -> float:
    """Escalated deduction of one open finding, at ``on_date`` or today.

    With ``on_date`` set (the history backfill path) the curve is measured
    against that date, findings not yet detected then are skipped, and the
    excepted check still uses today's exceptions — an exception's effect does
    not move backwards in time.
    """
    if on_date is not None:
        if finding.first_seen and finding.first_seen.date() > on_date:
            return 0.0
        days = (
            (finding.due_date - on_date).days if finding.due_date else None
        )
    else:
        days = finding.days_remaining
    if finding.is_excepted:
        # An accepted risk does not escalate. The curve stays a pure
        # function of days; the policy decision is made at the call site.
        days = None
    return base_weight(finding.severity) * escalation_multiplier(days)


def _dedup_open_findings(host: "Host") -> list["VulnFinding"]:
    """Deduped open findings for scoring: worst severity, soonest due date.

    Two separate reductions per group — the worst severity and the soonest
    due date come from *independent* rows. Keeping only the worst row would
    let a second scanner reporting the same CVE with a later date launder an
    overdue finding out of the score.
    """
    from .models import VulnFinding

    groups: dict[tuple[str, str] | str, list[VulnFinding]] = {}
    for finding in (
        VulnFinding.objects.filter(host=host, state=VulnFinding.State.OPEN)
        .select_related("exception")
        .iterator()
    ):
        key: tuple[str, str] | str = (
            (finding.scanner, finding.plugin_id_or_oid)
            if not finding.cve_id
            else finding.cve_id.strip().upper()
        )
        groups.setdefault(key, []).append(finding)

    deduped: list[VulnFinding] = []
    for members in groups.values():
        worst = max(members, key=lambda f: SEVERITY_RANK.get(f.severity, -1))
        dated = [m for m in members if m.due_date is not None]
        soonest = min(dated, key=lambda f: f.due_date) if dated else None
        if soonest is not None and soonest is not worst:
            # The two attributes come from different rows; splice the
            # soonest date onto the worst row in memory. The exception is
            # finding-specific and stays with the row it belongs to.
            worst.due_date = soonest.due_date
        deduped.append(worst)
    return deduped


def recompute_summary(host: "Host") -> "VulnSummary":
    """Recount findings + recompute score for one host.

    Walks every ``OPEN`` :class:`VulnFinding` for ``host``, dedupes by
    CVE (or by scanner+plugin when no CVE is set), buckets by severity,
    writes the counts to :class:`VulnSummary`, and stores the resulting
    score. ``info``-level findings don't affect the score but still get
    counted into the summary so the dashboard total matches the findings
    list.

    The counts and the score are now computed from two different
    reductions over the deduped set: the counts still bucket by worst
    severity per CVE (the dashboard total must keep matching the findings
    list), while the score weights each finding individually against its
    due date — two criticals on one host can sit at very different points
    on the clock.

    Idempotent — safe to call from any ingest path, or from a backfill
    script, or from a management command.
    """
    from .models import VulnFinding, VulnSummary

    deduped = _dedup_open_findings(host)

    counts: dict[str, int] = defaultdict(int)
    for finding in deduped:
        counts[finding.severity] += 1

    deduction = sum(_finding_deduction(finding) for finding in deduped)

    from django.utils.timezone import localdate

    today = localdate()
    overdue_count = 0
    due_soon_count = 0
    for finding in deduped:
        if finding.is_excepted or finding.due_date is None:
            # Excepted findings count toward neither — an accepted risk is
            # a policy decision, not a deadline.
            continue
        days = (finding.due_date - today).days
        if days < 0:
            overdue_count += 1
        elif days <= 14:
            due_soon_count += 1

    summary, _ = VulnSummary.objects.get_or_create(host=host)
    summary.critical = counts.get(VulnFinding.Severity.CRITICAL, 0)
    summary.high = counts.get(VulnFinding.Severity.HIGH, 0)
    summary.medium = counts.get(VulnFinding.Severity.MEDIUM, 0)
    summary.low = counts.get(VulnFinding.Severity.LOW, 0)
    summary.info = counts.get(VulnFinding.Severity.INFO, 0)
    summary.score = compute_escalated_score(deduction)
    summary.overdue_count = overdue_count
    summary.due_soon_count = due_soon_count
    summary.save(update_fields=[
        "critical", "high", "medium", "low", "info",
        "overdue_count", "due_soon_count",
        "score", "synced_at",
    ])
    return summary
