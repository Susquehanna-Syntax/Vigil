from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosts', '0009_hostfirewall'),
    ]

    operations = [
        migrations.AddField(
            model_name='host',
            name='reboot_required',
            field=models.BooleanField(default=False),
        ),
    ]
