from django.core.management.base import BaseCommand

from apps.catalog.sync import sync_all


class Command(BaseCommand):
    help = "Fetch the package indexes the fleet needs into the Store's catalog (ignores VIGIL_CATALOG_SYNC)."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="", help="only this source: winget, flatpak or dpkg")

    def handle(self, *args, source, **options):
        for row in sync_all(source):
            where = f"{row.source}{':' + row.release if row.release else ''}"
            if row.error:
                self.stdout.write(self.style.WARNING(f"{where}: {row.error}"))
            else:
                self.stdout.write(f"{where}: {row.count} packages")
