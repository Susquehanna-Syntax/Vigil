"""Make pretend hosts for the load harness (sprint M4 phase 01).

    python manage.py loadtest_seed --hosts 2000 --out /tmp/load-tokens.txt [--prefix load]
    python manage.py loadtest_seed --delete [--prefix load]

Each host is approved (online, managed) with its own agent token; the raw
tokens go to --out, one per line, readable by the owner only, for
scripts/loadtest.py. Every host is named <prefix>-NNNNN and tagged "loadtest",
and --delete removes only hosts that have both, so it cannot reach real ones.
"""
import os
import re
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.hosts.models import Host, hash_agent_token

PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,20}$")
BATCH = 500
TAG = "loadtest"


class Command(BaseCommand):
    help = "Make (or --delete) approved pretend hosts for scripts/loadtest.py."

    def add_arguments(self, parser):
        parser.add_argument("--hosts", type=int, default=0)
        parser.add_argument("--out")
        parser.add_argument("--prefix", default="load")
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, hosts, out, prefix, delete, **options):
        # No hyphen in the prefix: the harness takes the hostname as the first
        # two hyphen-separated parts of a token.
        if not PREFIX_RE.match(prefix or ""):
            raise CommandError("--prefix must be a lowercase letter then 1-20 letters or digits")
        if delete:
            doomed = [h.pk for h in Host.objects.filter(hostname__startswith=f"{prefix}-").only("pk", "tags")
                      if TAG in (h.tags or [])]
            for i in range(0, len(doomed), BATCH):
                Host.objects.filter(pk__in=doomed[i:i + BATCH]).delete()
            self.stdout.write(f"deleted {len(doomed)} hosts")
            return
        if hosts < 1 or not out:
            raise CommandError("give --hosts N (at least 1) and --out FILE, or --delete")
        if hosts > 99999:
            raise CommandError("at most 99999 hosts")
        if Host.objects.filter(hostname__startswith=f"{prefix}-").exists():
            raise CommandError(f"hosts named {prefix}-* already exist: run loadtest_seed --delete first")

        tokens = []
        with transaction.atomic():
            for start in range(1, hosts + 1, BATCH):
                rows = []
                for i in range(start, min(start + BATCH, hosts + 1)):
                    raw = f"{prefix}-{i:05d}-{secrets.token_hex(16)}"
                    tokens.append(raw)
                    # bulk_create skips Host.save, which is what hashes a token.
                    rows.append(Host(
                        hostname=f"{prefix}-{i:05d}", agent_token=hash_agent_token(raw),
                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
                        os="Debian GNU/Linux 12", kernel="6.1.0-loadtest", tags=[TAG]))
                Host.objects.bulk_create(rows)

        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # an existing file keeps its old mode through O_CREAT
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(tokens) + "\n")
        self.stdout.write(f"made {hosts} hosts; tokens in {out}")
