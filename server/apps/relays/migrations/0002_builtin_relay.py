"""The Built-in relay row.

It stands for "agents talk to the dashboard directly", and it exists as a row so
the edition limit counts it (Community: one connection point — either this or
one remote container) and so "direct connections off" is a flag on something
rather than an absence.
"""

from django.db import migrations


def create_builtin(apps, schema_editor):
    Relay = apps.get_model("relays", "Relay")
    if not Relay.objects.filter(kind="builtin").exists():
        Relay.objects.create(name="Built-in", kind="builtin", enabled=True)


class Migration(migrations.Migration):
    """Creates the Built-in relay. Reversing is a no-op: dropping the row would
    silently re-enable direct agent connections on a rolled-back deploy."""

    dependencies = (("relays", "0001_initial"),)

    operations = (migrations.RunPython(create_builtin, migrations.RunPython.noop),)
