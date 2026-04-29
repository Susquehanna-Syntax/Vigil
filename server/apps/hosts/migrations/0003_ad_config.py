from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosts', '0002_host_inventory'),
    ]

    operations = [
        migrations.CreateModel(
            name='ADConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ldap_url', models.CharField(blank=True, max_length=512)),
                ('bind_dn', models.CharField(blank=True, max_length=512)),
                ('bind_password_encrypted', models.BinaryField(blank=True, default=b'')),
                ('base_dn', models.CharField(blank=True, max_length=512)),
                ('computer_ou', models.CharField(blank=True, max_length=512)),
                ('enabled', models.BooleanField(default=False)),
                ('last_sync', models.DateTimeField(blank=True, null=True)),
                ('last_sync_status', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
