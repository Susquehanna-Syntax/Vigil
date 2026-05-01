from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("hosts", "0003_ad_config"),
    ]
    operations = [
        migrations.AddField(model_name="hostinventory", name="os_name",           field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name="hostinventory", name="os_version",        field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="hostinventory", name="kernel_version",    field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="hostinventory", name="architecture",      field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="hostinventory", name="uptime_seconds",    field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="hostinventory", name="last_logged_user",  field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="hostinventory", name="bios_version",      field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="hostinventory", name="bios_date",         field=models.CharField(blank=True, max_length=50)),
        migrations.AddField(model_name="hostinventory", name="system_timezone",   field=models.CharField(blank=True, max_length=80)),
    ]
