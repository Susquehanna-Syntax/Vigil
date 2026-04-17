"""Add TaskTemplate + TaskStep models; extend Task with multi-step fields."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── TaskTemplate ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="TaskTemplate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("slug", models.SlugField(max_length=200, unique=True)),
                ("source", models.CharField(
                    choices=[("local", "Local"), ("community", "Community")],
                    default="local",
                    max_length=20,
                )),
                ("upstream_version", models.CharField(blank=True, max_length=50)),
                ("steps", models.JSONField(default=list)),
                ("variables", models.JSONField(default=list)),
                ("risk_level", models.CharField(
                    choices=[("low", "Low"), ("standard", "Standard"), ("high", "High")],
                    default="standard",
                    max_length=10,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="task_templates",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"ordering": ["name"]},
        ),

        # ── Extend Task ───────────────────────────────────────────────────
        migrations.AddField(
            model_name="task",
            name="name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="task",
            name="steps",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="task",
            name="variables",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="task",
            name="template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks",
                to="tasks.tasktemplate",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="action",
            field=models.CharField(blank=True, max_length=100),
        ),

        # ── TaskStep ──────────────────────────────────────────────────────
        migrations.CreateModel(
            name="TaskStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("step_name", models.CharField(max_length=200)),
                ("step_action", models.CharField(blank=True, max_length=200)),
                ("state", models.CharField(
                    choices=[
                        ("pending", "Pending"),
                        ("running", "Running"),
                        ("ok", "OK"),
                        ("error", "Error"),
                        ("skipped", "Skipped"),
                    ],
                    default="pending",
                    max_length=10,
                )),
                ("output", models.TextField(blank=True)),
                ("exit_code", models.IntegerField(default=0)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("task", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="step_results",
                    to="tasks.task",
                )),
            ],
            options={"ordering": ["started_at"]},
        ),
    ]
