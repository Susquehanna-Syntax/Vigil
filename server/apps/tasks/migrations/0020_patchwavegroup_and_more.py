"""Waves gain a group.

Every wave that already exists moves into one "Default" group, so a deployment
that had a single global wave ladder keeps exactly the ladder it had, and
rollouts already in flight keep walking it.
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


def create_default_group(apps, schema_editor):
    Group = apps.get_model("tasks", "PatchWaveGroup")
    Wave = apps.get_model("tasks", "PatchWave")
    Rollout = apps.get_model("tasks", "PatchRollout")
    if not Wave.objects.exists() and not Rollout.objects.exists():
        return
    import uuid as _uuid
    group = Group.objects.create(id=_uuid.uuid4(), name="Default",
                                 description="", is_default=True)
    Wave.objects.filter(group__isnull=True).update(group=group)
    Rollout.objects.filter(wave_group__isnull=True).update(wave_group=group)


def drop_default_group(apps, schema_editor):
    Group = apps.get_model("tasks", "PatchWaveGroup")
    Rollout = apps.get_model("tasks", "PatchRollout")
    Wave = apps.get_model("tasks", "PatchWave")
    groups = Group.objects.filter(is_default=True)
    # PatchRollout.wave_group is PROTECT, so the references have to go first or
    # the whole reverse migration fails on any deployment that ever ran one.
    Rollout.objects.filter(wave_group__in=groups).update(wave_group=None)
    Wave.objects.filter(group__in=groups).update(group=None)
    groups.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('hosts', '0015_host_docker_snapshot_at'),
        ('tasks', '0019_taskdefinition_archived_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='PatchWaveGroup',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=120, unique=True)),
                ('description', models.TextField(blank=True, default='')),
                ('is_default', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='patchwave',
            name='uniq_patch_wave_order',
        ),
        migrations.AddField(
            model_name='patchrollout',
            name='wave_group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='rollouts', to='tasks.patchwavegroup'),
        ),
        migrations.AddField(
            model_name='patchwave',
            name='group',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='waves', to='tasks.patchwavegroup'),
        ),
        migrations.RunPython(create_default_group, drop_default_group),
        migrations.AddConstraint(
            model_name='patchwave',
            constraint=models.UniqueConstraint(fields=('group', 'order'), name='uniq_patch_wave_order'),
        ),
    ]
