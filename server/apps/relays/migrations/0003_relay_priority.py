"""The order the fallback list is handed to agents in (QA-54b)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("relays", "0002_builtin_relay"),)

    operations = (
        migrations.AddField(
            model_name="relay",
            name="priority",
            field=models.PositiveSmallIntegerField(default=100),
        ),
    )
