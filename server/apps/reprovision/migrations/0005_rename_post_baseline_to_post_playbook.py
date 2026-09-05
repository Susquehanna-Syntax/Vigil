from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("reprovision", "0004_installprofile_completion_tag_rows_and_more"),
        ("baselines", "0008_rename_baseline_to_playbook"),
    ]

    operations = [
        migrations.RenameField(
            model_name="rebuildjob",
            old_name="post_baseline",
            new_name="post_playbook",
        ),
    ]
