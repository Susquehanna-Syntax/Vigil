"""Re-seed built-in templates to pick up check-docker-updates.yaml.

Same upsert-by-name seeding as 0009 — re-running against the current
contents of ``builtin_templates/`` adds the new template and refreshes
the canonical copies of existing ones without touching users' forks.

Unlike 0009 this seed tolerates duplicate owner-less community rows for
the same name (installs that predate the GitHub-backed community tab can
carry both a locally-submitted copy and the 0009-seeded copy — 0009's
``update_or_create`` crashes on those with MultipleObjectsReturned).
Duplicates are collapsed onto the oldest row; ``TaskRun.definition`` and
``forked_from`` are both SET_NULL, so dropping the extras can't cascade
into task history or user forks.
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
        target = rows[0] if rows else TaskDefinition(
            name=spec["name"], owner=None, visibility="community"
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
        name="Check Docker images for updates", owner=None, visibility="community"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0009_seed_builtin_templates"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
