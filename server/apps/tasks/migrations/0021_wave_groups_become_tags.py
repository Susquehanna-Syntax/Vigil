"""Wave groups become tags rather than a record picked from a dropdown.

A group was a row, a wave pointed at exactly one, and the editor offered a
select. Everything else in Vigil selects things by tag, and a wave genuinely
can belong to more than one ladder — a Canary wave is often the first rung of
both the server and the workstation rollout, which a single FK cannot say.

Existing assignments carry across: a wave in a named group gains that group's
name as a group tag, and a rollout walking that group keeps walking it by tag.
Waves in the auto-created "Default" group become ungrouped, which is the same
set a rollout that names no group walks — so nothing changes shape.

Reversing restores the columns but not the assignments; the tags they came
from are still on the waves.
"""

from django.db import migrations, models


def groups_to_tags(apps, schema_editor):
    Wave = apps.get_model("tasks", "PatchWave")
    Rollout = apps.get_model("tasks", "PatchRollout")
    Tag = apps.get_model("hosts", "Tag")

    def tag_for(name):
        key = str(name).lower()
        tag, _ = Tag.objects.get_or_create(
            key=key, defaults={"name": str(name), "kind": "manual"})
        return tag

    for wave in Wave.objects.select_related("group").all():
        group = wave.group
        if group is None or group.is_default:
            continue
        wave.group_tags = [group.name]
        wave.save(update_fields=["group_tags"])
        wave.group_tag_rows.add(tag_for(group.name))

    for rollout in Rollout.objects.select_related("wave_group").all():
        group = rollout.wave_group
        if group is None or group.is_default:
            continue
        rollout.wave_group_tag = group.name
        rollout.save(update_fields=["wave_group_tag"])


class Migration(migrations.Migration):

    dependencies = [
        ("hosts", "0015_host_docker_snapshot_at"),
        ("tasks", "0020_patchwavegroup_and_more"),
    ]

    operations = [
        # New columns first: the data step below reads the old ones, so they
        # cannot be dropped until it has run.
        migrations.AddField(
            model_name="patchrollout",
            name="wave_group_tag",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="patchwave",
            name="group_tags",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="patchwave",
            name="group_tag_rows",
            field=models.ManyToManyField(
                blank=True, related_name="wave_groups", to="hosts.tag"),
        ),
        migrations.RunPython(groups_to_tags, migrations.RunPython.noop),
        # Order stops being unique: two ladders each want their own wave 1.
        # The API refuses a collision among waves sharing a group instead.
        migrations.RemoveConstraint(
            model_name="patchwave",
            name="uniq_patch_wave_order",
        ),
        migrations.RemoveField(model_name="patchwave", name="group"),
        migrations.RemoveField(model_name="patchrollout", name="wave_group"),
        migrations.DeleteModel(name="PatchWaveGroup"),
    ]
