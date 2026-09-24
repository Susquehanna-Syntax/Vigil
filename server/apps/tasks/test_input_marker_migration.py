"""Migration 0023 rewrites stored `{{ inputs.x }}` to `${{ inputs.x }}`.

Covers task YAML and cached parsed specs (including clearing the old-syntax
warnings), playbook-step and automation param overrides, idempotence on the
new form, and the rollback path back to the deprecated marker.
"""

import importlib
from pathlib import Path

from django.apps import apps
from django.test import TestCase

from apps.automations.models import Automation
from apps.playbooks.models import Playbook, PlaybookStep
from apps.tasks.models import TaskDefinition

_migration = importlib.import_module(
    "apps.tasks.migrations.0023_input_marker_rewrite"
)

_LEGACY_YAML = (
    "name: Marker migrate\n"
    "risk: standard\n"
    "actions:\n"
    "  - id: a\n"
    "    type: run_command\n"
    "    params:\n"
    "      command: 'docker pull {{ inputs.pkg }}'\n"
)

_LEGACY_SPEC = {
    "name": "Marker migrate",
    "risk": "standard",
    "actions": [
        {
            "id": "a",
            "type": "run_command",
            "params": {"command": "docker pull {{ inputs.pkg }}"},
        }
    ],
    "warnings": [],
}


def _make_definition(yaml_source=_LEGACY_YAML, parsed_spec=_LEGACY_SPEC):
    return TaskDefinition.objects.create(
        owner=None,
        name="Marker migrate",
        yaml_source=yaml_source,
        parsed_spec=parsed_spec,
    )


class InputMarkerMigrationTests(TestCase):
    def test_definition_yaml_and_spec_are_rewritten(self):
        _NEW_YAML = _LEGACY_YAML.replace("{{ inputs.pkg }}", "${{ inputs.pkg }}")
        definition = _make_definition()
        _migration.forward(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.yaml_source, _NEW_YAML)
        self.assertEqual(
            definition.parsed_spec["actions"][0]["params"]["command"],
            "docker pull ${{ inputs.pkg }}",
        )

    def test_old_syntax_warning_is_cleared(self):
        spec = dict(_LEGACY_SPEC)
        spec["warnings"] = [
            (
                "params.command: {{ inputs.pkg }} is the old input syntax "
                "— write ${{ inputs.pkg }}"
            ),
            "other",
        ]
        definition = _make_definition(parsed_spec=spec)
        _migration.forward(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.parsed_spec["warnings"], ["other"])

    def test_overrides_are_rewritten(self):
        override = {"0": {"service_name": "{{ inputs.svc }}"}}
        definition = _make_definition()
        playbook = Playbook.objects.create(name="Marker playbook")
        step = PlaybookStep.objects.create(
            playbook=playbook, definition=definition, order=0,
            params_override=override,
        )
        automation = Automation.objects.create(
            name="Marker automation", trigger="event",
            action_kind="task", task_definition=definition,
            params_override=override,
        )
        _migration.forward(apps, None)
        step.refresh_from_db()
        automation.refresh_from_db()
        self.assertEqual(
            step.params_override, {"0": {"service_name": "${{ inputs.svc }}"}}
        )
        self.assertEqual(
            automation.params_override, {"0": {"service_name": "${{ inputs.svc }}"}}
        )

    def test_new_markers_and_bare_braces_are_untouched(self):
        yaml_source = (
            "name: Marker migrate\n"
            "risk: standard\n"
            "actions:\n"
            "  - id: a\n"
            "    type: run_command\n"
            "    params:\n"
            "      command: 'docker ps --format {{.Names}} -exec rm {} \\; "
            "pull ${{ inputs.pkg }}'\n"
        )
        definition = _make_definition(
            yaml_source=yaml_source, parsed_spec={"actions": []}
        )
        _migration.forward(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.yaml_source, yaml_source)
        self.assertEqual(definition.parsed_spec, {"actions": []})

    def test_backward_restores_the_old_form(self):
        definition = _make_definition()
        _migration.forward(apps, None)
        _migration.backward(apps, None)
        definition.refresh_from_db()
        self.assertEqual(definition.yaml_source, _LEGACY_YAML)
        self.assertEqual(definition.parsed_spec, _LEGACY_SPEC)

    def test_builtin_update_container_uses_new_marker(self):
        template = (
            Path(__file__).parent / "builtin_templates" / "update-container.yaml"
        ).read_text()
        self.assertIn("${{ inputs.container_name }}", template)
        self.assertNotIn('"{{ inputs.container_name }}"', template)
