from django.db import migrations, models


class Migration(migrations.Migration):
    """The schema half of 0005_unify_global_site.

    Kept separate so the ALTER TABLE runs in its own transaction: 0005 rewrites
    Site rows, which leaves deferred FK trigger events pending on that table,
    and PostgreSQL will not ALTER a table in that state.
    """

    dependencies = [("business_sites", "0006_site_capability")]

    operations = [
        migrations.RemoveField(model_name="site", name="is_default"),
    ]

    # Reverse restores the column so 0005's split() has somewhere to write.
    # Django derives that automatically from RemoveField's field state.
