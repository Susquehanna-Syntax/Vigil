"""Load an offline vulnerability-data bundle (OSV, KEV, EPSS) — air-gapped installs."""
from django.core.management.base import BaseCommand, CommandError

from apps.vulns.vulndata import import_bundle


class Command(BaseCommand):
    help = "Load OSV records, the CISA KEV catalogue and FIRST EPSS scores from a bundle."

    def add_arguments(self, parser):
        parser.add_argument("path", help="A .zip, .tar.gz or directory (see apps/vulns/vulndata.py)")

    def handle(self, *args, path, **options):
        try:
            counts = import_bundle(path)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Loaded {counts['osv']} OSV records, {counts['kev']} KEV entries, "
                          f"{counts['epss']} EPSS scores.")
