from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0010_maintenance_windows"),
    ]

    operations = [
        migrations.AddField(
            model_name="alert",
            name="notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="alert",
            name="resolve_pending",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
