from django.db import migrations, models
import django.db.models.deletion


def link_baselines(apps, schema_editor):
    """Resolve each automation's stored name to a real row. Safe to do now:
    Baseline.name is still globally unique at this point in history."""
    Automation = apps.get_model("automations", "Automation")
    Baseline = apps.get_model("baselines", "Baseline")
    for auto in Automation.objects.exclude(baseline_name="").iterator():
        match = Baseline.objects.filter(name__iexact=auto.baseline_name).first()
        if match is not None:
            auto.baseline_id = match.id
            auto.save(update_fields=["baseline"])


def unlink_baselines(apps, schema_editor):
    Automation = apps.get_model("automations", "Automation")
    for auto in Automation.objects.exclude(baseline__isnull=True).iterator():
        auto.baseline_name = auto.baseline.name
        auto.save(update_fields=["baseline_name"])


class Migration(migrations.Migration):
    """Add the FK and backfill it. Dropping baseline_name lives in 0005.

    On a fresh database link_baselines() touches no rows, so this would pass
    CI either way. On a real upgrade it updates every automation that names a
    baseline, and those UPDATEs leave deferred FK trigger events pending on
    automations_automation — after which PostgreSQL refuses to ALTER that
    table in the same transaction.
    """

    dependencies = [
        ("automations", "0003_automation_params_override"),
        ("baselines", "0003_baselinestep_params_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="automation",
            name="baseline",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="automations", to="baselines.baseline",
            ),
        ),
        migrations.RunPython(link_baselines, unlink_baselines),
    ]
