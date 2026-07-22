from django.db import migrations


def create_default_site(apps, schema_editor):
    Site = apps.get_model("business_sites", "Site")
    Site.objects.create(
        name="Default",
        slug="default",
        is_default=True,
    )


def remove_default_site(apps, schema_editor):
    Site = apps.get_model("business_sites", "Site")
    Site.objects.filter(is_default=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("business_sites", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_default_site, remove_default_site),
    ]
