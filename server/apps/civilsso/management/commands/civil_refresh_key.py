from django.core.management.base import BaseCommand, CommandError

from apps.civilsso import client


class Command(BaseCommand):
    help = "Re-fetch Civil's JWT public key (run after rotating Civil's key)."

    def handle(self, *args, **opts):
        if not client.enabled():
            raise CommandError("CIVIL_URL is not configured")
        pem = client.get_public_key(force_fetch=True)
        if not pem:
            raise CommandError("could not fetch a key from Civil")
        self.stdout.write(self.style.SUCCESS("Civil public key refreshed"))
