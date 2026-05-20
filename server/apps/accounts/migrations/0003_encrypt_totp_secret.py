from django.db import migrations, models

from apps.hosts.crypto import encrypt_secret


def encrypt_existing_secrets(apps, schema_editor):
    """Encrypt any TOTP secrets stored in plaintext by earlier versions."""
    UserProfile = apps.get_model("accounts", "UserProfile")
    for profile in UserProfile.objects.exclude(totp_secret=""):
        profile.totp_secret_encrypted = encrypt_secret(profile.totp_secret)
        profile.save(update_fields=["totp_secret_encrypted"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_totp_replay_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="totp_secret_encrypted",
            field=models.BinaryField(blank=True, default=b""),
        ),
        migrations.RunPython(encrypt_existing_secrets, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="userprofile",
            name="totp_secret",
        ),
    ]
