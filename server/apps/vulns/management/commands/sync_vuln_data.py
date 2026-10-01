"""Fetch vulnerability data from its sources — connected installs."""
from django.core.management.base import BaseCommand

from apps.vulns.vulndata import fetch_osv


class Command(BaseCommand):
    help = "Download OSV per ecosystem (and refresh KEV) from the public sources."

    def add_arguments(self, parser):
        parser.add_argument("--ecosystem", action="append", default=[],
                            help='OSV ecosystem, e.g. "Debian", "Ubuntu", "Alpine". Repeatable.')
        parser.add_argument("--kev", action="store_true", help="Refresh the CISA KEV catalogue too.")

    def handle(self, *args, ecosystem, kev, **options):
        for eco in ecosystem:
            self.stdout.write(f"{eco}: {fetch_osv(eco)} OSV records")
        if kev:
            from apps.vulns.kev import fetch_live
            self.stdout.write(f"KEV: {fetch_live()} entries")
