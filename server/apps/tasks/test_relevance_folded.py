"""Free-text ``relevance:`` folds into the description at parse time.

Phase 02b of M6: ``relevance:`` is a free-text string nothing evaluates;
next to the real ``relevant:`` block it only confuses authors. Old YAML
keeps working — its text now lives in the description — and nothing new
writes the field.
"""

from importlib import import_module
from pathlib import Path

from django.apps import apps
from django.test import TestCase

from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

_migration = import_module("apps.tasks.migrations.0029_relevance_into_description")

OLD_YAML = (
    "name: Legacy relevance\n"
    "description: exercises the pre-relevant schema\n"
    "relevance: web servers\n"
    "risk: high\n"
    "actions:\n"
    "  - id: bounce\n"
    "    type: restart_service\n"
    "    params: { service_name: nginx }\n"
)

FOLDED_DESCRIPTION = (
    "exercises the pre-relevant schema\n\nRelevant to: web servers"
)

RELEVANCE_WARNING = (
    "relevance: is free text and is now part of the description — "
    "use relevant: to decide where a task applies"
)


class RelevanceFoldParserTests(TestCase):
    def test_old_yaml_folds_into_description(self):
        spec = parse_and_validate(OLD_YAML)
        self.assertEqual(spec["description"], FOLDED_DESCRIPTION)
        self.assertEqual(spec["relevance"], "")
        self.assertIn(RELEVANCE_WARNING, spec["warnings"])

    def test_reparse_does_not_duplicate(self):
        # Re-parsing a task that still carries the `relevance:` line but
        # whose description already ends with the folded sentence must not
        # append it twice.
        already_folded = (
            "name: Legacy relevance\n"
            "description: |\n"
            "  exercises the pre-relevant schema\n"
            "\n"
            "  Relevant to: web servers\n"
            "relevance: web servers\n"
            "risk: high\n"
            "actions:\n"
            "  - id: bounce\n"
            "    type: restart_service\n"
            "    params: { service_name: nginx }\n"
        )
        spec = parse_and_validate(already_folded)
        self.assertEqual(spec["description"], FOLDED_DESCRIPTION)
        self.assertEqual(spec["description"].count("Relevant to: web servers"), 1)


class RelevanceFoldMigrationTests(TestCase):
    def test_migration_moves_text(self):
        definition = TaskDefinition.objects.create(
            owner=None,
            name="Legacy relevance",
            description="exercises the pre-relevant schema",
            relevance="web servers",
            risk_level="high",
            yaml_source=OLD_YAML,
            parsed_spec=parse_and_validate(OLD_YAML),
        )
        _migration.fold(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.description, FOLDED_DESCRIPTION)
        self.assertEqual(definition.relevance, "")
        self.assertEqual(definition.parsed_spec["description"], FOLDED_DESCRIPTION)
        self.assertEqual(definition.parsed_spec["relevance"], "")

    def test_migration_skips_already_folded_rows(self):
        definition = TaskDefinition.objects.create(
            owner=None,
            name="Already folded",
            description=FOLDED_DESCRIPTION,
            relevance="web servers",
            risk_level="high",
            yaml_source=OLD_YAML,
            parsed_spec={
                "description": FOLDED_DESCRIPTION,
                "relevance": "web servers",
            },
        )
        _migration.fold(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.description, FOLDED_DESCRIPTION)
        self.assertEqual(definition.relevance, "")
        self.assertEqual(definition.parsed_spec["description"], FOLDED_DESCRIPTION)
        self.assertEqual(definition.parsed_spec["relevance"], "")

    def test_builtin_templates_have_no_relevance_line(self):
        builtin_dir = Path(__file__).parent / "builtin_templates"
        for yaml_path in sorted(builtin_dir.glob("*.yaml")):
            src = yaml_path.read_text()
            self.assertNotIn("\nrelevance:", src, yaml_path.name)
            spec = parse_and_validate(src)
            self.assertEqual(spec["relevance"], "", yaml_path.name)
