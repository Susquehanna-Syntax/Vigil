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
    """Data only. Dropping is_default lives in 0007 on purpose.

    Deleting and updating Site rows queues deferred FK trigger events from
    every table that cascades off Site. PostgreSQL then refuses to ALTER that
    table in the same transaction:

        cannot ALTER TABLE "business_sites_site" because it has pending
        trigger events

    Splitting the schema change into its own migration gives it a fresh
    transaction, after this one's triggers have fired. SQLite does not
    enforce this, which is why the local suite never caught it.
    """

    dependencies = [("business_sites", "0004_scope_assignments")]

    operations = [
        migrations.RunPython(unify, split),
    ]
