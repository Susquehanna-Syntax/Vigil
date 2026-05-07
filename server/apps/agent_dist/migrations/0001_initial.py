import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AgentBinary",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("platform", models.CharField(
                    choices=[("linux-amd64", "Linux (x86-64)"), ("linux-arm64", "Linux (ARM64)")],
                    max_length=30,
                    unique=True,
                )),
                ("version", models.CharField(blank=True, max_length=50)),
                ("binary", models.FileField(upload_to="agent_binaries/")),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Agent binary",
                "verbose_name_plural": "Agent binaries",
                "ordering": ["platform"],
            },
        ),
    ]
