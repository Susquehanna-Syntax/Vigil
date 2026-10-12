"""Seed the built-in tasks the template playbooks are built from.

Same upsert-by-name seeding as 0039 — re-running against the current contents
of ``builtin_templates/`` adds the new tasks and refreshes the canonical copies
of existing ones without touching users' forks.

The new tasks are the steps of the bundled library playbooks: a Trivy scan, the
three service steps a recovery playbook needs, and the pair that keeps software
current. None of them is new behaviour — every action already existed — so what
ships here is the friendly, discoverable name for it.
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


NEW = ("Run a Trivy scan", "Restart a service", "Check a service",
       "Start a service", "Refresh software inventory", "Upgrade outdated apps")


def unseed(apps, schema_editor):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    TaskDefinition.objects.filter(name__in=NEW, owner=None, visibility="community").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0040_inputs"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
