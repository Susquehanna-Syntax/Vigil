"""Rename the alert event an automation can trigger on.

``alert_fired`` named the state transition, which asked an operator to catch a
moment Vigil mostly suppresses: a re-fire inside the flap window is not
announced, and a still-breaching alert is not announced again. What actually
happens once, reliably, is Vigil *sending* the alert — so that is what the
trigger is now called.

Existing rows are moved rather than dropped: an automation someone built keeps
working, under a name that describes when it runs.
"""

from django.db import migrations


def to_alert_sent(apps, schema_editor):
    apps.get_model("automations", "Automation").objects.filter(
        event="alert_fired").update(event="alert_sent")


def to_alert_fired(apps, schema_editor):
    apps.get_model("automations", "Automation").objects.filter(
        event="alert_sent").update(event="alert_fired")


class Migration(migrations.Migration):

    dependencies = [("automations", "0012_automation_allow_high_risk")]

    operations = [migrations.RunPython(to_alert_sent, to_alert_fired)]
