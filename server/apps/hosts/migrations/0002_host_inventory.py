import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosts', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='HostInventory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mac_addresses', models.JSONField(blank=True, default=dict)),
                ('ram_total_bytes', models.BigIntegerField(blank=True, null=True)),
                ('cpu_model', models.CharField(blank=True, max_length=255)),
                ('cpu_cores', models.IntegerField(blank=True, null=True)),
                ('service_tag', models.CharField(blank=True, max_length=120)),
                ('manufacturer', models.CharField(blank=True, max_length=120)),
                ('model_name', models.CharField(blank=True, max_length=160)),
                ('disks', models.JSONField(blank=True, default=list)),
                ('custom_columns', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('host', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='inventory', to='hosts.host')),
            ],
        ),
    ]
