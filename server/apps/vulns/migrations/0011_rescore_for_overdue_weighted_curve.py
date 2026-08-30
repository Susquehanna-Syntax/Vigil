"""Re-score history and every summary under the overdue-weighted curve.

2026.9.1 re-anchored the escalation curve: a finding still inside its
remediation window is discounted steeply, a finding past its due date is
amplified. The score now answers "am I keeping my remediation promises?"
rather than "how many findings does this host have?".

Two things need rewriting for that to be true of an existing install:

* **Summaries.** ``VulnSummary.score`` is stored, not derived on read, so
  until something recomputes it every host keeps its old score. Nothing else
  triggers a recount until the next scan, which could be days away.
* **History.** Same reasoning as migration 0010 — without a backfill the
  sparkline steps at the release boundary for no operational reason. The same
  honest limitation applies and is worth restating: historical rows record
  only a score, so this replays *today's* findings against each historical
  date using ``first_seen``. Findings remediated before today are gone and
  cannot be reconstructed.

This also corrects an inversion that 0010 shipped with. It routed excepted
findings through the no-deadline path, which was harmless while both weighed
1.0 and is not harmless now: under the new curve that path is the
conservative weight, so an accepted risk would have scored *worse* than a
finding with runway. Both paths now share ``scoring.deduction_for``.

Reversing is a no-op. The old scores are not recoverable from the new ones,
and a backwards migration that silently left them re-scored is more honest
than one that pretends to restore them.
"""

from datetime import date

from django.db import migrations
from django.utils.timezone import localdate

BATCH = 500


def _rescore_history(apps, _schema_editor):
    from apps.vulns.scoring import SEVERITY_RANK, deduction_for

    History = apps.get_model("vulns", "VulnScoreHistory")
    Finding = apps.get_model("vulns", "VulnFinding")
    VulnException = apps.get_model("vulns", "VulnException")

    today = localdate()
    excepted_ids = {
        row.finding_id
        for row in VulnException.objects.filter(expires_on__gte=today)
        .only("finding_id")
    }

    host_ids = list(History.objects.values_list("host_id", flat=True).distinct())
    batch = []
    touched = 0

    for host_id in host_ids:
        findings = list(
            Finding.objects.filter(host_id=host_id, state="open").only(
                "id", "severity", "cve_id", "scanner", "plugin_id_or_oid",
                "first_seen", "due_date",
            )
        )
        # Same dedup as the live path: worst severity and soonest due date come
        # from independent rows, so a second scanner reporting the same CVE with
        # a later date cannot launder an overdue finding out of the score.
        groups: dict = {}
        for f in findings:
            key = (f.cve_id.strip().upper() if f.cve_id
                   else (f.scanner, f.plugin_id_or_oid))
            groups.setdefault(key, []).append(f)
        deduped = []
        for members in groups.values():
            worst = max(members, key=lambda f: SEVERITY_RANK.get(f.severity, -1))
            dated = [m for m in members if m.due_date is not None]
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
                    continue  # not detected yet at that historical date
                days = (f.due_date - row_date).days if f.due_date else None
                deduction += deduction_for(
                    f.severity, days_remaining=days,
                    is_excepted=f.id in excepted_ids)
            row.score = 100 - int(round(deduction))
            batch.append(row)
            if len(batch) >= BATCH:
                History.objects.bulk_update(batch, ["score"])
                touched += len(batch)
                batch = []

    if batch:
        History.objects.bulk_update(batch, ["score"])
        touched += len(batch)
    if touched:
        print(f"\n  vulns: re-scored {touched} history row(s).")


def _rescore_summaries(apps, _schema_editor):
    """Recompute every stored summary score.

    Uses the live ``recompute_summary`` rather than a frozen reimplementation:
    it is the definition of the score, and a copy here would be a third place
    for the rule to drift. Summaries are derived data — recomputing them from
    the findings that are still in the table is always correct, which is not
    true of the history above.
    """
    from apps.hosts.models import Host
    from apps.vulns.scoring import recompute_summary

    Summary = apps.get_model("vulns", "VulnSummary")
    host_ids = list(Summary.objects.values_list("host_id", flat=True))
    done = 0
    for host in Host.objects.filter(id__in=host_ids).iterator():
        recompute_summary(host)
        done += 1
    if done:
        print(f"  vulns: re-scored {done} host summary/summaries.\n")


def _noop(apps, schema_editor):
    """Deliberately does nothing — see the module docstring."""


class Migration(migrations.Migration):

    dependencies = [("vulns", "0010_recompute_score_history_escalated")]

    operations = [
        migrations.RunPython(_rescore_history, _noop),
        migrations.RunPython(_rescore_summaries, _noop),
    ]
