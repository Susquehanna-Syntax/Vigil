"""Rewrite the deprecated ``{{ inputs.x }}`` marker to ``${{ inputs.x }}``.

Braces inside task text collide with YAML, shell, PowerShell and Go-template
syntax, so the input marker gained a ``$`` prefix; the old bare-brace form is
deprecated for one release. This one-time migration rewrites every stored
occurrence — task YAML, cached parsed specs, playbook-step and automation
param overrides — so existing rows stop warning and no stored text depends
on the old form.

The marker regexes are copied from ``apps.tasks.spec`` rather than imported:
a migration must keep meaning the same thing when ``spec.py`` changes later,
and data migrations run against the historical app registry, not the current
code.
"""

import re
from typing import ClassVar

from django.db import migrations

_LEGACY = re.compile(r"(?<!\$)\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_NEW = re.compile(r"\$\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def to_new(text: str) -> str:
    return _LEGACY.sub(lambda m: "${{ inputs." + m.group(1) + " }}", text)


def to_legacy(text: str) -> str:
    return _NEW.sub(lambda m: "{{ inputs." + m.group(1) + " }}", text)


def _rewrite(value, fn):
    """Apply ``fn`` to every string in nested dicts/lists; pass the rest through."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _rewrite(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, fn) for v in value]
    return value


def _rewrite_definitions(apps, fn):
    TaskDefinition = apps.get_model("tasks", "TaskDefinition")
    for row in TaskDefinition.objects.all():
        yaml_source = fn(row.yaml_source)
        parsed_spec = _rewrite(row.parsed_spec, fn)
        if fn is to_new and isinstance(parsed_spec.get("warnings"), list):
            parsed_spec["warnings"] = [
                w for w in parsed_spec["warnings"]
                if not (isinstance(w, str) and "old input syntax" in w)
            ]
        if yaml_source == row.yaml_source and parsed_spec == row.parsed_spec:
            continue
        row.yaml_source = yaml_source
        row.parsed_spec = parsed_spec
        row.save(update_fields=["yaml_source", "parsed_spec"])


def _rewrite_overrides(apps, fn):
    PlaybookStep = apps.get_model("baselines", "PlaybookStep")
    for row in PlaybookStep.objects.all():
        params_override = _rewrite(row.params_override, fn)
        if params_override == row.params_override:
            continue
        row.params_override = params_override
        row.save(update_fields=["params_override"])

    Automation = apps.get_model("automations", "Automation")
    for row in Automation.objects.all():
        params_override = _rewrite(row.params_override, fn)
        if params_override == row.params_override:
            continue
        row.params_override = params_override
        row.save(update_fields=["params_override"])


def forward(apps, schema_editor):
    _rewrite_definitions(apps, to_new)
    _rewrite_overrides(apps, to_new)


def backward(apps, schema_editor):
    _rewrite_definitions(apps, to_legacy)
    _rewrite_overrides(apps, to_legacy)


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tasks", "0022_seed_update_container"),
        ("baselines", "0011_playbook_archived_at"),
        ("automations", "0015_fold_legacy_filters_into_conditions"),
    ]

    operations: ClassVar[list] = [
        migrations.RunPython(forward, backward),
    ]
