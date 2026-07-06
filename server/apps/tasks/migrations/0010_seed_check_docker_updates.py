"""Re-seed built-in templates to pick up check-docker-updates.yaml.

Same upsert-by-name seeding as 0009 — re-running against the current
contents of ``builtin_templates/`` adds the new template and refreshes
the canonical copies of existing ones without touching users' forks.
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
        TaskDefinition.objects.update_or_create(
            name=spec["name"],
            owner=None,
            visibility="community",
            defaults={
                "description": spec["description"],
                "relevance": spec["relevance"],
                "risk_level": spec["risk"],
                "yaml_source": src,
                "parsed_spec": spec,
            },
        )


def unseed(apps, schema_editor):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    TaskDefinition.objects.filter(
        name="Check Docker images for updates", owner=None, visibility="community"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0009_seed_builtin_templates"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
