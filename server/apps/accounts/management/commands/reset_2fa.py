"""Clear a user's second factors so they can enrol again at next sign-in.

Every account must prove a second factor to sign in (login_mfa), so a lost
phone or key would otherwise lock that person out for good. This is the way
back, and it needs shell access to the server on purpose: nothing reachable
from the browser can strip a factor without presenting one.

    docker compose exec web python manage.py reset_2fa <username>
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import SecurityKey, UserProfile


class Command(BaseCommand):
    help = "Remove a user's TOTP secret and security keys; they enrol again at next sign-in."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, username, **options):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No user named {username!r}.")
        keys, _ = SecurityKey.objects.filter(user=user).delete()
        UserProfile.objects.filter(user=user).update(
            totp_secret_encrypted=b"", totp_confirmed_at=None,
            last_totp_code="", last_totp_used_at=None,
        )
        try:
            from apps_business.audits.models import record
        except ImportError:
            pass
        else:
            record("account.second_factor_reset", user=user, target=username,
                   auth_method="manage.py")
        self.stdout.write(f"Reset two-factor sign-in for {username}: TOTP cleared, "
                          f"{keys} security key(s) removed. They set up TOTP at next sign-in.")
