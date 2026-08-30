"""Seed the bundled KEV catalogue, then give every existing finding a due date.

Both steps are best-effort by design. A missing or malformed catalogue file
must not fail a deployment — the worst case is that findings fall back to their
severity tier instead of the shorter KEV tier, which is a scoring nuance, not
an outage.
"""

from datetime import timedelta

from django.db import migrations

BATCH = 1000

# Mirrors apps.vulns.remediation._SEVERITY_POLICY_FIELD. Duplicated on purpose:
# a migration must keep working against the schema as it was at this point in
# history, so it cannot import code that will keep evolving.
_SEVERITY_FIELD = {
    "critical": "critical_days",
    "high": "high_days",
    "medium": "medium_days",
    "low": "low_days",
}


def load_kev(apps, schema_editor):
    from apps.vulns import kev as kev_module

    KevEntry = apps.get_model("vulns", "KevEntry")
    try:
        import json

        with open(kev_module.BUNDLED_CATALOG, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        entries = kev_module.parse_catalog(payload)
    except Exception:  # noqa: BLE001 - never fail a migration over metadata
        return

    rows = [
        KevEntry(
            cve_id=e["cve_id"],
            date_added=e["date_added"],
            cisa_due_date=e.get("cisa_due_date"),
            ransomware=e.get("ransomware", False),
            name=e.get("name", ""),
            source="bundled",
        )
        for e in entries
        if e.get("date_added")
    ]
    KevEntry.objects.bulk_create(rows, batch_size=BATCH, ignore_conflicts=True)


def backfill_due_dates(apps, schema_editor):
    VulnFinding = apps.get_model("vulns", "VulnFinding")
    KevEntry = apps.get_model("vulns", "KevEntry")
    RemediationPolicy = apps.get_model("vulns", "RemediationPolicy")

    policy = RemediationPolicy.objects.order_by("pk").first()
    if policy is None:
        policy = RemediationPolicy.objects.create()

    pending = VulnFinding.objects.filter(due_date__isnull=True).exclude(severity="info")
    if not pending.exists():
        return

    # One query for every KEV row we could possibly need, rather than a lookup
    # per finding. On a fleet with tens of thousands of findings the per-row
    # version takes minutes and can time out a deploy.
    kev_map = {
        k.cve_id: k
        for k in KevEntry.objects.only("cve_id", "cisa_due_date")
    }

    batch = []
    for finding in pending.iterator(chunk_size=BATCH):
        start = finding.first_seen.date() if finding.first_seen else None
        if start is None:
            continue
        entry = kev_map.get((finding.cve_id or "").strip().upper())
        if entry is not None:
            if policy.use_cisa_due_date and entry.cisa_due_date:
                finding.due_date = entry.cisa_due_date
            else:
                finding.due_date = start + timedelta(days=policy.kev_days)
        else:
            field = _SEVERITY_FIELD.get(finding.severity, "critical_days")
            finding.due_date = start + timedelta(days=getattr(policy, field))
        batch.append(finding)
        if len(batch) >= BATCH:
            VulnFinding.objects.bulk_update(batch, ["due_date"])
            batch = []
    if batch:
        VulnFinding.objects.bulk_update(batch, ["due_date"])


def noop(apps, schema_editor):
    """Reverse is a no-op: dropping the column removes the data anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ("vulns", "0007_keventry_remediationpolicy_vulnfinding_due_date_and_more"),
    ]

    operations = [
        migrations.RunPython(load_kev, noop),
        migrations.RunPython(backfill_due_dates, noop),
    ]
