"""Match every host's inventory against the local OSV copy now."""
from django.core.management.base import BaseCommand

from apps.vulns.matcher import match_all


class Command(BaseCommand):
    help = "Re-match the Apps inventory of every host against the loaded OSV data."

    def handle(self, *args, **options):
        self.stdout.write(f"Matched {match_all()} hosts.")
