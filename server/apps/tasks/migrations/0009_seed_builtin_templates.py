"""Seed built-in community task templates from apps/tasks/builtin_templates/*.yaml.

Built-in templates are owner-less (``owner=None``) community definitions so
every install ships with a working, forkable library. Re-running is safe:
each template is upserted by name, so editing a YAML and shipping a new
migration refreshes the canonical copy without touching users' own forks
(which always carry an ``owner``).
"""

from pathlib import Path

from django.db import migrations

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_templates"


def _iter_specs():
    # Imported here (not at module top) so the parser is only pulled in when
    # the migration actually runs, keeping the migration importable in isolation.
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
    for _src, spec in _iter_specs():
        TaskDefinition.objects.filter(
            name=spec["name"], owner=None, visibility="community"
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0008_task_hidden"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
