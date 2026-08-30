from django.core.management.base import BaseCommand

from apps.vulns import kev


class Command(BaseCommand):
    help = "Load the bundled CISA KEV catalogue into the database (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="Read a catalogue from this path instead of the bundled snapshot.",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            help="Fetch from CISA instead (requires VIGIL_KEV_LIVE_REFRESH).",
        )

    def handle(self, *args, **opts):
        if opts["live"]:
            written = kev.fetch_live()
            source = "CISA"
        else:
            from pathlib import Path

            path = Path(opts["path"]) if opts["path"] else None
            written = kev.load_bundled(path)
            source = str(path or kev.BUNDLED_CATALOG)
        self.stdout.write(self.style.SUCCESS(f"Loaded {written} KEV entries from {source}"))
