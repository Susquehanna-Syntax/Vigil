"""``migrate``, one container at a time.

Every Vigil container (web, celery-worker, celery-beat, link) migrates on
start, and on a first boot they start together. Two migrations running at
once collide: the second CREATE TABLE fails on the type the first just made,
and that container exits. A Postgres advisory lock makes the others wait;
by the time each gets the lock the schema is current and it has nothing to
do. Other databases (SQLite in development) migrate straight away.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

#: Any constant works as long as nothing else in Vigil takes the same lock.
LOCK_ID = 0x56494749  # "VIGI"


class Command(BaseCommand):
    help = "Run migrate, waiting for any other container that is migrating."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
            return
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            if not cur.fetchone()[0]:
                self.stdout.write("Another container is migrating; waiting for it.")
                cur.execute("SELECT pg_advisory_lock(%s)", [LOCK_ID])
        try:
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
