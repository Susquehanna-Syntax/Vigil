"""Baseline became Playbook.

The name implied a one-shot thing applied at enrollment, which is exactly the
bug it caused: nobody expected it to keep applying to hosts that gained a
matching tag later. Renames only — every row survives, and the app label stays
"baselines" so historical migrations keep resolving.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    # Every migration that still names the old model has to have run before the
    # rename lands, or Django schedules one of them afterwards and it fails
    # resolving "baselines.baseline" against a model that no longer exists.
    dependencies = [
        ("baselines", "0007_baseline_community_uid"),
        ("hosts", "0001_initial"),
        ("automations", "0004_automation_baseline_fk"),
        ("business_sites", "0004_scope_assignments"),
        ("reprovision", "0001_initial"),
        ("tasks", "0015_patchrollout_action_kind_patchrollout_baseline_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="baselinestep",
            name="uniq_baseline_step_order",
        ),
        migrations.RenameModel(old_name="Baseline", new_name="Playbook"),
        migrations.RenameModel(old_name="BaselineStep", new_name="PlaybookStep"),
        migrations.RenameField(
            model_name="playbookstep", old_name="baseline", new_name="playbook",
        ),
        migrations.AlterField(
            model_name="playbookstep",
            name="playbook",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE, related_name="steps",
                to="baselines.playbook",
            ),
        ),
        migrations.AlterField(
            model_name="playbook",
            name="target_tag_rows",
            field=models.ManyToManyField(
                blank=True, related_name="playbooks", to="hosts.tag",
            ),
        ),
        migrations.AddConstraint(
            model_name="playbookstep",
            constraint=models.UniqueConstraint(
                fields=("playbook", "order"), name="uniq_playbook_step_order",
            ),
        ),
    ]
