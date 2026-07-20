"""Singleton AiSettings → multiple AiProvider rows. The old row, if it held a
real config, becomes a provider named "Default"."""

from django.db import migrations, models


def forward(apps, schema_editor):
    AiSettings = apps.get_model("aisuggest", "AiSettings")
    AiProvider = apps.get_model("aisuggest", "AiProvider")
    old = AiSettings.objects.first()
    if old and (old.base_url or old.model):
        AiProvider.objects.create(
            name="Default",
            kind=old.provider,
            base_url=old.base_url,
            model=old.model,
            api_key_encrypted=old.api_key_encrypted,
            enabled=old.enabled,
            order=0,
        )


def backward(apps, schema_editor):
    AiSettings = apps.get_model("aisuggest", "AiSettings")
    AiProvider = apps.get_model("aisuggest", "AiProvider")
    first = AiProvider.objects.order_by("order").first()
    row = AiSettings.objects.first() or AiSettings()
    if first:
        row.provider = first.kind
        row.base_url = first.base_url
        row.model = first.model
        row.api_key_encrypted = first.api_key_encrypted
        row.enabled = first.enabled
        row.save()


class Migration(migrations.Migration):

    dependencies = [("aisuggest", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="AiProvider",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("kind", models.CharField(default="openai", max_length=20)),
                ("base_url", models.URLField(blank=True, default="")),
                ("model", models.CharField(blank=True, default="", max_length=200)),
                ("api_key_encrypted", models.BinaryField(blank=True, default=b"")),
                ("enabled", models.BooleanField(default=True)),
                ("order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("order", "created_at")},
        ),
        migrations.RunPython(forward, backward),
        migrations.DeleteModel(name="AiSettings"),
    ]
