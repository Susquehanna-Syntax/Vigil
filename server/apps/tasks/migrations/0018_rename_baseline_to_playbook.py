"""Rename the baseline references on TaskRun and PatchRollout to playbook.

Both models store the old vocabulary in a CharField as well as an FK, so the
stored values move with the columns — a run recorded as source="baseline"
would otherwise stop matching Source.PLAYBOOK and vanish from history filters.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    apps.get_model("tasks", "TaskRun").objects.filter(
        source="baseline").update(source="playbook")
    apps.get_model("tasks", "PatchRollout").objects.filter(
        action_kind="baseline").update(action_kind="playbook")


def backwards(apps, schema_editor):
    apps.get_model("tasks", "TaskRun").objects.filter(
        source="playbook").update(source="baseline")
    apps.get_model("tasks", "PatchRollout").objects.filter(
        action_kind="playbook").update(action_kind="baseline")


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0017_taskdefinition_community_uid"),
        ("baselines", "0008_rename_baseline_to_playbook"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="patchrollout",
            name="rollout_has_exactly_one_target",
        ),
        migrations.RenameField(
            model_name="taskrun", old_name="baseline", new_name="playbook",
        ),
        migrations.RenameField(
            model_name="patchrollout", old_name="baseline", new_name="playbook",
        ),
        migrations.AlterField(
            model_name="taskrun",
            name="playbook",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=models.deletion.SET_NULL,
                related_name="runs", to="baselines.playbook",
            ),
        ),
        migrations.AlterField(
            model_name="patchrollout",
            name="playbook",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE, related_name="rollouts",
                to="baselines.playbook", null=True, blank=True,
            ),
        ),
        migrations.AlterField(
            model_name="taskrun",
            name="source",
            field=models.CharField(
                choices=[("manual", "Manual deploy"), ("automation", "Automation"),
                         ("playbook", "Playbook"), ("reprovision", "Reprovision")],
                default="manual", max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="patchrollout",
            name="action_kind",
            field=models.CharField(
                choices=[("task", "Task definition"), ("playbook", "Playbook")],
                default="task", max_length=12,
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AddConstraint(
            model_name="patchrollout",
            constraint=models.CheckConstraint(
                condition=models.Q(definition__isnull=False, playbook__isnull=True)
                | models.Q(definition__isnull=True, playbook__isnull=False),
                name="rollout_has_exactly_one_target",
            ),
        ),
    ]
