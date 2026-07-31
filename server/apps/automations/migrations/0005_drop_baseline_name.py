from django.db import migrations


class Migration(migrations.Migration):
    """The schema half of 0004_automation_baseline_fk.

    Separate so the ALTER TABLE gets its own transaction, after the backfill
    in 0004 has committed and its deferred FK trigger events have fired.
    """

    dependencies = [("automations", "0004_automation_baseline_fk")]

    operations = [
        migrations.RemoveField(model_name="automation", name="baseline_name"),
    ]
