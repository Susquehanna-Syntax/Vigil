from django.db import migrations


def forward(apps, schema_editor):
    # Offline alerts open before notified_at existed would otherwise all be
    # sent by the first pass of the late-notice sweep (alerts.tasks), some for
    # hosts down for months. Count them as already sent.
    Alert = apps.get_model("alerts", "Alert")
    for alert in Alert.objects.filter(rule=None, notified_at=None,
                                      message__startswith="Host offline:",
                                      state__in=["firing", "acknowledged"]):
        alert.notified_at = alert.fired_at
        alert.save(update_fields=["notified_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0011_alert_notified_at_resolve_pending"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
