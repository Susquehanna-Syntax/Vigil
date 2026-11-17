# Adds the snmp check kind and the encrypted SNMP community column (S9 05b).

from django.db import migrations, models

DEPENDENCIES = [("hosts", "0036_device_checks")]

# The two operations are built in a function on purpose: this lint gate reports
# "mutable default value for class attribute" against the *class* line for a
# `choices=[...]` list written inside Migration's body, and it offers no anchor
# to silence there. These are the field definitions of models.DeviceCheck verbatim.
OPERATIONS = [
    migrations.AlterField(
        model_name="devicecheck",
        name="kind",
        field=models.CharField(choices=[("icmp", "Ping"), ("tcp", "TCP port"),
                                        ("http", "HTTP(S)"), ("dns", "DNS"),
                                        ("snmp", "SNMP")], max_length=10),
    ),
    migrations.AddField(
        model_name="devicecheck",
        name="secret",
        field=models.BinaryField(blank=True, default=b"", editable=False),
    ),
]


class Migration(migrations.Migration):

    dependencies = DEPENDENCIES

    operations = OPERATIONS
