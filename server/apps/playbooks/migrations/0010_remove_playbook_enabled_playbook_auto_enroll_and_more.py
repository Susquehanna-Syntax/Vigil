"""enabled becomes auto_enroll, and auto_enroll starts off for everyone.

Deliberately a drop-and-add rather than a rename. `enabled` defaulted to True,
so renaming the column would land every playbook already in a deployment with
unattended enrolment switched ON, and the first reconcile pass would dispatch
each of them to its whole target set at once. Nobody asked for that by having
created a playbook under the old meaning of the flag.

Auto-enrolment is now something an operator turns on per playbook, behind a
confirmation, after setting a completion tag.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('baselines', '0009_alter_playbook_created_by_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='playbook',
            name='enabled',
        ),
        migrations.AddField(
            model_name='playbook',
            name='auto_enroll',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='playbook',
            name='completion_tag',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
    ]
