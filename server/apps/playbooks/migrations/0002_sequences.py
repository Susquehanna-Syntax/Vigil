"""Baselines become named sequences: name + ordered BaselineStep rows replace
the single definition FK. Existing rows keep working — each becomes a
one-step sequence named after its definition."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def forwards(apps, schema_editor):
    Baseline = apps.get_model("baselines", "Baseline")
    BaselineStep = apps.get_model("baselines", "BaselineStep")
    seen = set()
    for b in Baseline.objects.select_related("definition"):
        base = (b.definition.name if b.definition else "Baseline")[:110]
        name, n = base, 2
        while name.lower() in seen:
            name = f"{base} ({n})"
            n += 1
        seen.add(name.lower())
        b.name = name
        b.save(update_fields=["name"])
        if b.definition_id:
            BaselineStep.objects.create(baseline=b, definition_id=b.definition_id,
                                        order=0)


def backwards(apps, schema_editor):
    Baseline = apps.get_model("baselines", "Baseline")
    for b in Baseline.objects.prefetch_related("steps"):
        first = b.steps.order_by("order").first()
        if first:
            b.definition_id = first.definition_id
            b.save(update_fields=["definition"])


class Migration(migrations.Migration):

    dependencies = [
        ("baselines", "0001_initial"),
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="baseline",
            name="name",
            field=models.CharField(max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="baseline",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="BaselineStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(default=0)),
                ("baseline", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="steps", to="baselines.baseline")),
                ("definition", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="baseline_steps", to="tasks.taskdefinition")),
            ],
            options={"ordering": ("order",)},
        ),
        migrations.AddConstraint(
            model_name="baselinestep",
            constraint=models.UniqueConstraint(
                fields=("baseline", "order"), name="uniq_baseline_step_order"),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="baseline", name="definition"),
        migrations.AlterField(
            model_name="baseline",
            name="name",
            field=models.CharField(max_length=120, unique=True),
        ),
    ]
