from django.db import migrations


def unify(apps, schema_editor):
    Site = apps.get_model("business_sites", "Site")
    # Order matters: name and slug are both unique, so the row added by
    # 0003_global_site has to go before the default row takes its name.
    Site.objects.filter(slug="global").delete()
    Site.objects.filter(is_default=True).update(
        is_global=True, name="Global", slug="global",
        description="Applies in every site unless suppressed.",
    )


def split(apps, schema_editor):
    Site = apps.get_model("business_sites", "Site")
    Site.objects.filter(is_global=True).update(
        is_default=True, name="Default", slug="default")


class Migration(migrations.Migration):

    dependencies = [("business_sites", "0004_scope_assignments")]

    operations = [
        migrations.RunPython(unify, split),
        migrations.RemoveField(model_name="site", name="is_default"),
    ]
