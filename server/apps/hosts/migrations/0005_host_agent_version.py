from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hosts", "0004_hostinventory_extended_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="agent_version",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
    ]
