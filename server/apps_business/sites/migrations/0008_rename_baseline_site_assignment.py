from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("business_sites", "0007_drop_site_is_default"),
        ("baselines", "0008_rename_baseline_to_playbook"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="BaselineSiteAssignment",
            new_name="PlaybookSiteAssignment",
        ),
        migrations.RenameField(
            model_name="playbooksiteassignment",
            old_name="baseline",
            new_name="playbook",
        ),
    ]
