"""Re-seed built-in templates to pick up the three hunt templates.

Same upsert-by-name seeding as 0022 — re-running against the current
contents of ``builtin_templates/`` adds the new templates
(``hunt-log4j.yaml``, ``hunt-listening-port.yaml``,
``hunt-package-version.yaml``) and refreshes the canonical copies of
existing ones without touching users' forks.

Duplicate owner-less community rows for the same name are collapsed onto
the oldest row; ``TaskRun.definition`` and ``forked_from`` are both
SET_NULL, so dropping the extras can't cascade into task history or user
forks.
"""

from pathlib import Path

from django.db import migrations

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_templates"


def _iter_specs():
    from apps.tasks.spec import parse_and_validate

    for yaml_path in sorted(BUILTIN_DIR.glob("*.yaml")):
        src = yaml_path.read_text()
        yield src, parse_and_validate(src)


def seed(apps, schema_editor):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    for src, spec in _iter_specs():
        rows = list(
            TaskDefinition.objects.filter(
                name=spec["name"], owner=None, visibility="community"
            ).order_by("created_at")  # pk is a UUID — created_at is creation order
        )
        for extra in rows[1:]:
            extra.delete()
        target = (
            rows[0]
            if rows
            else TaskDefinition(name=spec["name"], owner=None, visibility="community")
        )
        target.description = spec["description"]
        target.relevance = spec["relevance"]
        target.risk_level = spec["risk"]
        target.yaml_source = src
        target.parsed_spec = spec
        target.save()


def unseed(apps, schema_editor):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    TaskDefinition.objects.filter(
        name__in=[
            "Find vulnerable log4j jars",
            "Find hosts listening on a port",
            "Find hosts with a package below a version",
        ],
        owner=None,
        visibility="community",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0026_task_expires_at"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
