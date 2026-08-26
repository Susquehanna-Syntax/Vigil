"""Remediation deadlines and the escalation curve.

Two things live here, and they are deliberately together:

* :func:`compute_due_date` — when a finding must be fixed by, derived once at
  detection time from the active :class:`~apps.vulns.models.RemediationPolicy`
  and CISA KEV membership.
* :func:`escalation_multiplier` — how much harder a finding hits the score as
  that date approaches and passes.

Both are plain functions over plain data so they can be tested without a
database, and so there is exactly one implementation of each rule. The scanners
never call either one directly: ``VulnFinding.save()`` derives the due date, so
a scanner added later cannot forget to.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import RemediationPolicy, VulnFinding


# ── Severity → policy field ──────────────────────────────────────────────────
#
# Maps a finding's severity onto the policy column that governs it. ``info`` is
# absent on purpose: informational findings get no deadline at all.
_SEVERITY_POLICY_FIELD = {
    "critical": "critical_days",
    "high": "high_days",
    "medium": "medium_days",
    "low": "low_days",
}


def compute_due_date(
    finding: "VulnFinding",
    policy: "RemediationPolicy | None" = None,
    kev_entry=None,
    today: date | None = None,
) -> date | None:
    """Return the date ``finding`` must be remediated by, or ``None``.

    ``policy`` and ``kev_entry`` are passed in rather than looked up so a bulk
    caller (the backfill migration, ``recompute_summary``) can fetch the policy
    once and the KEV rows in a single query instead of one query per finding.
    When ``policy`` is omitted the active row is loaded — convenient for the
    single-finding path in ``save()``, wrong for a loop.

    ``None`` comes back for ``info`` findings, which have no deadline and are
    excluded from overdue counts entirely.
    """
    if finding.severity == "info":
        return None

    if policy is None:
        from .models import RemediationPolicy

        policy = RemediationPolicy.get_active()

    # A brand-new unsaved row has no auto_now_add value yet, so fall back to
    # today. Once saved, first_seen is authoritative and never moves, which is
    # what keeps a deadline stable across re-scans.
    start = finding.first_seen.date() if finding.first_seen else (today or date.today())

    if kev_entry is not None:
        # CISA sets its own per-CVE deadline in the KEV catalogue. Prefer it
        # when the operator has left that switch on and the entry carries one;
        # otherwise the policy's KEV tier still applies, which is shorter than
        # any severity tier.
        if policy.use_cisa_due_date and kev_entry.cisa_due_date:
            return kev_entry.cisa_due_date
        return start + timedelta(days=policy.kev_days)

    field = _SEVERITY_POLICY_FIELD.get(finding.severity)
    if field is None:
        # An unrecognised severity is not a crash — a scanner could grow a new
        # one. Treat it as the most conservative tier we have.
        field = "critical_days"
    return start + timedelta(days=getattr(policy, field))


# ── The escalation curve ─────────────────────────────────────────────────────
#
# THIS TABLE IS THE TUNING POINT FOR THE WHOLE PRODUCT. Changing a number here
# re-weights every host's score at once. It is expressed as ordered breakpoints
# rather than a chain of ifs so the whole shape is readable at a glance:
# flat while there is runway, rising as the date nears, a hard step once past
# due, and a second step for findings that have been ignored for a month.
#
# Each pair is (minimum days_remaining for this band, multiplier). Evaluated
# top-down; the first band whose threshold is met wins.
_ESCALATION_BANDS: tuple[tuple[int, float], ...] = (
    (31, 1.0),    # more than a month of runway — no penalty
    (15, 1.25),   # 15..30 days — approaching
    (1, 1.6),     # 1..14 days — close
    (0, 2.0),     # due today
    (-30, 3.0),   # 1..30 days overdue
)
_ESCALATION_BADLY_OVERDUE = 4.0  # more than 30 days overdue


def escalation_multiplier(days_remaining: int | None) -> float:
    """Weight multiplier for a finding based on its distance from its due date.

    ``None`` means the finding has no deadline (an ``info`` finding, or a row
    predating the backfill) and is scored at its base weight.

    This is a pure function of days. Whether a finding is *excepted* is not its
    business — the caller drops excepted findings to 1.0, so that an accepted
    risk is visibly a policy decision at the call site rather than a hidden
    branch in here.
    """
    if days_remaining is None:
        return 1.0
    for threshold, multiplier in _ESCALATION_BANDS:
        if days_remaining >= threshold:
            return multiplier
    return _ESCALATION_BADLY_OVERDUE
