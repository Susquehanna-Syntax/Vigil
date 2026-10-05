"""Group every finding by its fix, and re-score every host by fix group (M9).

Until now a host's score and severity counts deduped by CVE, so a package
with a hundred CVEs deducted a hundred times. They now count fix groups
(apps/vulns/fixgroups.py): one per package-and-fixed-version, with groups
that share a CVE merged. This sets ``fix_key`` on the findings that predate
it and rewrites each ``VulnSummary`` so an existing install does not wait for
its next scan to read correctly. History rows are left as they are: they
recorded what the score was.

Reversing is a no-op — the old counts cannot be told apart from the new.
"""

from django.db import migrations
from django.utils.timezone import localdate


def _forward(apps, _schema_editor):
    from apps.vulns.fixgroups import fix_key_for, group
    from apps.vulns.scoring import deduction_for, reduce_group

    Finding = apps.get_model("vulns", "VulnFinding")
    Summary = apps.get_model("vulns", "VulnSummary")
    VulnException = apps.get_model("vulns", "VulnException")

    for f in Finding.objects.filter(fix_key="").iterator():
        Finding.objects.filter(pk=f.pk).update(fix_key=fix_key_for(
            f.package_name, f.fixed_version, f.scanner, f.plugin_id_or_oid, f.affected_path))

    today = localdate()
    excepted = set(VulnException.objects.filter(expires_on__gte=today)
                   .values_list("finding_id", flat=True))
    for summary in Summary.objects.all():
        rows = [reduce_group(g) for g in group(
            Finding.objects.filter(host_id=summary.host_id, state="open"))]
        counts = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
        deduction = 0.0
        overdue = due_soon = 0
        for f in rows:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            days = (f.due_date - today).days if f.due_date else None
            is_excepted = f.id in excepted
            deduction += deduction_for(f.severity, days_remaining=days, is_excepted=is_excepted)
            if not is_excepted and days is not None:
                if days < 0:
                    overdue += 1
                elif days <= 14:
                    due_soon += 1
        Summary.objects.filter(pk=summary.pk).update(
            critical=counts["critical"], high=counts["high"], medium=counts["medium"],
            low=counts["low"], info=counts["info"], score=100 - int(round(deduction)),
            overdue_count=overdue, due_soon_count=due_soon)


class Migration(migrations.Migration):

    dependencies = [("vulns", "0015_vulnfinding_fix_key")]

    operations = [migrations.RunPython(_forward, migrations.RunPython.noop)]
