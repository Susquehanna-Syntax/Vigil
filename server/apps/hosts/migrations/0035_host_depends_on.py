from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hosts", "0034_signing_key_rotation"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="depends_on",
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL,
                                    related_name="dependents", to="hosts.host"),
        ),
    ]
