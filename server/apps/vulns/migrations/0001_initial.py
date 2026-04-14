import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("hosts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="VulnSummary",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("last_scan_at", models.DateTimeField(blank=True, null=True)),
                ("scanner_scan_id", models.IntegerField(blank=True, null=True)),
                ("critical", models.IntegerField(default=0)),
                ("high", models.IntegerField(default=0)),
                ("medium", models.IntegerField(default=0)),
                ("low", models.IntegerField(default=0)),
                ("info", models.IntegerField(default=0)),
                ("synced_at", models.DateTimeField(auto_now=True)),
                (
                    "host",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vuln_summary",
                        to="hosts.host",
                    ),
                ),
            ],
            options={
                "verbose_name": "Vulnerability Summary",
                "verbose_name_plural": "Vulnerability Summaries",
                "ordering": ["-critical", "-high"],
            },
        ),
    ]
