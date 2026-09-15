"""Move the old fixed filters into conditions.

The event trigger grew five separate filter boxes over time — rule, severity,
tags, host, text match — in three different visual idioms, and then conditions
arrived doing the same job in one. Connor's read: "pretty messy and can be
collapsed into conditions with one trigger."

Three of the five say something a condition says, so they move here and the
editor drops the boxes. Doing it in a migration rather than leaving the fields
populated is the point: an automation whose filter still applied but no longer
appeared anywhere would be the worst of both.

event_rule and event_host stay. They reference a row by id, and a condition
matches a name — folding them would turn "this exact rule" into "anything
called that", which breaks the day someone renames it.

Reversible in shape, not in fact: the down migration puts the fields back
where it can read them off unambiguously, and leaves conditions alone
otherwise. Nobody loses a filter either way.
"""

from django.db import migrations


def fold(apps, schema_editor):
    from apps.automations.conditions import fold_legacy

    Automation = apps.get_model("automations", "Automation")
    for auto in Automation.objects.all():
        folded = fold_legacy(
            min_severity=auto.min_severity,
            event_tags=auto.event_tags,
            match_text=auto.match_text,
            match_field=auto.match_field,
            match_mode=auto.match_mode,
        )
        if not folded:
            continue
        # The old filters were ANDed with each other and with anything already
        # in conditions, so "all" is the only logic that preserves behaviour.
        # An automation already set to "any" would have its meaning changed by
        # appending, so those keep their boxes rather than being rewritten.
        if auto.conditions and auto.condition_logic != "all":
            continue
        auto.conditions = list(auto.conditions or []) + folded
        auto.condition_logic = "all"
        auto.min_severity = ""
        auto.event_tags = []
        auto.match_text = ""
        auto.save(update_fields=["conditions", "condition_logic",
                                 "min_severity", "event_tags", "match_text"])


def unfold(apps, schema_editor):
    Automation = apps.get_model("automations", "Automation")
    for auto in Automation.objects.all():
        keep = []
        for cond in auto.conditions or []:
            field, op, value = cond.get("field"), cond.get("op"), cond.get("value")
            if field == "severity" and op == "gte" and not auto.min_severity:
                auto.min_severity = value
            elif field == "host_tags" and op == "in" and not auto.event_tags:
                auto.event_tags = [p.strip() for p in str(value).split(",") if p.strip()]
            elif field in ("text", "rule", "message") and not auto.match_text:
                auto.match_text = value
                auto.match_field = {"text": "any"}.get(field, field)
                auto.match_mode = op
            else:
                keep.append(cond)
        auto.conditions = keep
        auto.save(update_fields=["conditions", "min_severity", "event_tags",
                                 "match_text", "match_field", "match_mode"])


class Migration(migrations.Migration):

    dependencies = [("automations", "0014_automation_condition_logic_automation_conditions")]

    operations = [migrations.RunPython(fold, unfold)]
