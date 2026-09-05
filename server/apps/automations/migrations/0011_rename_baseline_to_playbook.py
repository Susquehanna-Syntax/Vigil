from django.db import migrations, models


def forwards(apps, schema_editor):
    apps.get_model("automations", "Automation").objects.filter(
        action_kind="baseline").update(action_kind="playbook")


def backwards(apps, schema_editor):
    apps.get_model("automations", "Automation").objects.filter(
        action_kind="playbook").update(action_kind="baseline")


class Migration(migrations.Migration):

    dependencies = [
        ("automations", "0010_automation_community_uid"),
        ("baselines", "0008_rename_baseline_to_playbook"),
    ]

    operations = [
        migrations.RenameField(
            model_name="automation", old_name="baseline", new_name="playbook",
        ),
        migrations.AlterField(
            model_name="automation",
            name="action_kind",
            field=models.CharField(
                choices=[("task", "Task definition"), ("playbook", "Playbook")],
                max_length=12,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
