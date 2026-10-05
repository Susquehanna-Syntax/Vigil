"""Fold free-text ``relevance:`` into the description.

``relevance:`` is a free-text string nothing evaluates; next to the real
``relevant:`` block it only confuses authors. Old YAML keeps working — the
community repo and every saved task still carry the line — but its text now
lives in the description, and nothing new writes the field.

For every ``TaskDefinition`` with a non-empty ``relevance``, the text is
appended to ``description`` (and to ``parsed_spec["description"]``) as
``\n\nRelevant to: <text>``, unless the description already ends with exactly
that line — re-parsing a migrated task must not append twice. ``relevance``
is then blanked in both places. The model column is kept; it simply stays
empty. Reverse is a no-op: the original text cannot be recovered.

After the fold, the built-in templates are re-seeded with the same
upsert-by-name pattern as 0027 so installed copies pick up the rewritten
YAML (their ``relevance:`` lines were deleted and folded into the
descriptions).
"""

from pathlib import Path
from typing import ClassVar

from django.db import migrations

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_templates"


def _folded_description(description: str, relevance: str) -> str:
    suffix = f"\n\nRelevant to: {relevance}"
    if not description.endswith(suffix):
        return (description + suffix) if description else suffix.lstrip("\n")
    return description


def fold(apps, schema_editor):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    for row in TaskDefinition.objects.filter(relevance__gt=""):
        relevance = row.relevance
        row.description = _folded_description(row.description, relevance)
        parsed_spec = row.parsed_spec
        if isinstance(parsed_spec, dict):
            parsed_spec["description"] = _folded_description(
                parsed_spec.get("description", ""), relevance
            )
            parsed_spec["relevance"] = ""
        row.relevance = ""
        row.parsed_spec = parsed_spec
        row.save()
    _reseed_builtin_templates(apps)


def noop(apps, schema_editor):
    pass


def _reseed_builtin_templates(apps):
    from apps.tasks.spec import parse_and_validate

    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    for yaml_path in sorted(BUILTIN_DIR.glob("*.yaml")):
        src = yaml_path.read_text()
        spec = parse_and_validate(src)
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


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tasks", "0028_not_applicable_state"),
    ]

    operations: ClassVar[list] = [
        migrations.RunPython(fold, noop),
    ]
