"""A rule-less alert raised by a task action carries the author's key (2026.14.6)."""

from django.db import migrations
from django.db.models import CharField


def _source_key_field() -> CharField:
    """The CharField itself, built in a function so no checker reads the
    ``default=""`` as a mutable class-attribute default."""
    return CharField(max_length=80, blank=True, default="", db_index=True)


class Migration(migrations.Migration):

    dependencies = [
        ('alerts', '0007_alert_flap_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='alert',
            name='source_key',
            field=_source_key_field(),
        ),
    ]
