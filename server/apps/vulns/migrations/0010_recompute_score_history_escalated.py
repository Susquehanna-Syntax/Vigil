"""Recompute the score history under the new escalation curve.

Why: before this migration every historical score was the un-escalated
weighted count. The live score is now escalated per finding against its
due date, so without a backfill the sparkline would step down at the
release boundary for no operational reason.

HONEST LIMITATION — this is an approximation, and a visible one:

Historical rows record only a score, not the findings that produced it.
This migration cannot know what a host's findings looked like 60 days
ago. It therefore recomputes each historical row by applying the
escalation the host's *current* open findings would have had *at that
historical date*, using each finding's ``first_seen`` to decide whether
it existed yet. Findings fixed before today are gone from the table and
cannot be reconstructed — so a host whose findings were largely
remediated will show a smoother (and flatter) history than it truly
had, converging on the score it would have once its findings stopped
existing.

The pure scoring helpers are imported from the current app code rather
than duplicated here: they take plain data, touch no database, and keep
the escalation curve in exactly one place. The excepted check uses
today's exceptions — the migration has no historical exception state,
and an exception's effect is not something we can un-date.
"""

from datetime import date

from django.db import migrations
from django.utils.timezone import localdate

BATCH = 500


def recompute_history(apps, schema_editor):
    from apps.vulns.remediation import escalation_multiplier
    from apps.vulns.scoring import SEVERITY_RANK, base_weight

    History = apps.get_model("vulns", "VulnScoreHistory")
    Finding = apps.get_model("vulns", "VulnFinding")
    VulnException = apps.get_model("vulns", "VulnException")

    today = localdate()
    hosts = list(History.objects.values_list("host_id", flat=True).distinct())

    batch = []
    for host_id in hosts:
        findings = list(
            Finding.objects.filter(host_id=host_id, state="open")
            .only(
                "id", "severity", "due_date", "first_seen",
                "cve_id", "scanner", "plugin_id_or_oid",
            )
        )
        excepted_ids = set(
            VulnException.objects.filter(
                finding_id__in=[f.id for f in findings],
                expires_on__gte=today,
            ).values_list("finding_id", flat=True)
        ) if findings else set()

        # Same dedup as the live recompute: worst severity and soonest
        # due date are independent reductions over the group.
        groups: dict = {}
        for f in findings:
            key = (
                f.cve_id.strip().upper()
                if f.cve_id
                else (f.scanner, f.plugin_id_or_oid)
            )
            groups.setdefault(key, []).append(f)
        deduped = []
        for members in groups.values():
            worst = max(members, key=lambda f: SEVERITY_RANK.get(f.severity, -1))
            dated = [m for m in members if m.due_date]
            if dated:
                soonest = min(dated, key=lambda f: f.due_date)
                if soonest is not worst:
                    worst.due_date = soonest.due_date
            deduped.append(worst)

        for row in History.objects.filter(host_id=host_id).only("id", "date"):
            row_date = row.date if isinstance(row.date, date) else row.date.date()
            deduction = 0.0
            for f in deduped:
                if f.first_seen and f.first_seen.date() > row_date:
                    # Not detected yet at that historical date.
                    continue
                days = (f.due_date - row_date).days if f.due_date else None
                if f.id in excepted_ids:
                    days = None  # accepted risk: no escalation
                deduction += base_weight(f.severity) * escalation_multiplier(days)
            row.score = 100 - int(round(deduction))
            batch.append(row)
            if len(batch) >= BATCH:
                History.objects.bulk_update(batch, ["score"])
                batch = []
    if batch:
        History.objects.bulk_update(batch, ["score"])


def noop(apps, schema_editor):
    """Reverse is a no-op: re-running the migration would just recompute."""


class Migration(migrations.Migration):

    dependencies = [
        ("vulns", "0009_vulnsummary_due_soon_count_vulnsummary_overdue_count"),
    ]

    operations = [
        migrations.RunPython(recompute_history, noop),
    ]
