"""Restore an archive made by the nightly backup (sprint M3 phase 4).

This REPLACES the database. Copy the archive out of the backups volume, stop
everything that writes, then run it in a one-off web container (media is
writable there) with the secrets volume mounted writable beside the
read-only one:

    docker compose cp celery-worker:/var/lib/vigil/backups/<archive> ./backup.tar.gz
    docker compose stop web celery-worker celery-beat link
    docker compose run --rm --no-deps --entrypoint "" \\
        -e VIGIL_SECRETS_DIR=/restore-secrets -v <project>_secrets:/restore-secrets \\
        -v "$PWD/backup.tar.gz:/tmp/backup.tar.gz:ro" \\
        web python manage.py restore_backup /tmp/backup.tar.gz --yes
    docker compose up -d          # migrations run on start

(<project> is the compose project name, usually the directory's name.)

What comes back: the database, the uploaded media, and DJANGO_SECRET_KEY and
VIGIL_SIGNING_KEY_SEED (without them agents refuse the server and stored
credentials cannot be decrypted). The database password does not: pg_dump
carries no roles, so the server keeps the password this install was made
with, and restoring the old one would lock Vigil out of it. A value set in
.env still wins over the restored files, as it always does.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.instance.backup import FORMAT_VERSION, PG_DUMP_TIMEOUT

#: Restored from the archive. Never postgres_password (see the module doc).
RESTORED_SECRETS = ("django_secret_key", "signing_key_seed")


class Command(BaseCommand):
    help = "Restore a backup archive: database, media and the signing/secret keys."

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--yes", action="store_true", help="do not ask before replacing the database")
        parser.add_argument("--skip-secrets", action="store_true",
                            help="leave the secrets volume alone (it already holds this install's keys)")

    def handle(self, archive, yes=False, skip_secrets=False, **opts):
        path = Path(archive)
        if not path.is_file():
            raise CommandError(f"No archive at {path}")
        if not yes:
            db_name = settings.DATABASES["default"]["NAME"]
            answer = input(f"This REPLACES database {db_name!r}. Type its name to go on: ")
            if answer.strip() != str(db_name):
                raise CommandError("Aborted.")
        work = Path(tempfile.mkdtemp(prefix="vigil-restore-"))
        try:
            with tarfile.open(path, "r:gz") as tar:
                # The "data" filter refuses absolute paths, "..", links out of
                # the tree and device files: an archive cannot write elsewhere.
                tar.extractall(work, filter="data")
            manifest = json.loads((work / "MANIFEST.json").read_text())
            if manifest.get("format") != FORMAT_VERSION:
                raise CommandError(f"Unknown backup format {manifest.get('format')!r}")
            self.stdout.write(f"Backup of Vigil {manifest.get('vigil_version')} taken {manifest.get('taken_at')}")
            self._restore_database(work / manifest["database"])
            if (work / "media").is_dir():
                self._restore_media(work / "media")
            if not skip_secrets and (work / "secrets").is_dir():
                self._restore_secrets(work / "secrets")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.stdout.write(self.style.SUCCESS(
            "Restored. Start Vigil again: migrations bring an older backup up to this version."))

    def _restore_database(self, dump: Path):
        db = settings.DATABASES["default"]
        connections.close_all()
        if db["ENGINE"].endswith("sqlite3"):
            if dump.name != "db.sqlite3":
                raise CommandError("This archive holds a Postgres dump; this install runs SQLite.")
            import sqlite3
            src = sqlite3.connect(str(dump))
            dst = sqlite3.connect(str(db["NAME"]))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            self.stdout.write("  database restored")
            return
        if dump.name != "db.dump":
            raise CommandError("This archive holds a SQLite database; this install runs Postgres.")
        env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
        conn = ["--host", str(db.get("HOST") or "localhost"), "--port", str(db.get("PORT") or 5432),
                "--username", str(db["USER"]), "--dbname", str(db["NAME"])]

        def psql(sql):
            done = subprocess.run(["psql", *conn, "-v", "ON_ERROR_STOP=1", "-c", sql],
                                  env=env, capture_output=True, text=True)
            if done.returncode != 0:
                raise CommandError(f"psql failed: {done.stderr.strip()[-1500:]}")

        # An empty schema with TimescaleDB loaded, then the extension's own
        # restore mode around pg_restore, as its documentation prescribes.
        psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
             "CREATE EXTENSION IF NOT EXISTS timescaledb;")
        psql("SELECT timescaledb_pre_restore();")
        done = subprocess.run(["pg_restore", *conn, "--no-owner", str(dump)],
                              env=env, capture_output=True, text=True, timeout=PG_DUMP_TIMEOUT)
        psql("SELECT timescaledb_post_restore();")
        if done.returncode != 0:
            # TimescaleDB dumps restore with a few harmless complaints (its own
            # catalog objects already exist), so the exit code alone cannot
            # decide. What decides is whether the schema came back.
            self.stderr.write("pg_restore reported: " + done.stderr.strip()[-1500:])
        check = subprocess.run(["psql", *conn, "-tAc", "SELECT count(*) FROM django_migrations"],
                               env=env, capture_output=True, text=True)
        if check.returncode != 0 or not check.stdout.strip().isdigit() or int(check.stdout) == 0:
            raise CommandError("The database did not come back: no migrations recorded after the restore.")
        self.stdout.write("  database restored")

    def _restore_media(self, media: Path):
        root = Path(settings.MEDIA_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(media, root, dirs_exist_ok=True)
        self.stdout.write("  media restored")

    def _restore_secrets(self, secrets: Path):
        from vigil import bootsecrets

        target = Path(bootsecrets.secrets_dir())
        if not os.access(target, os.W_OK):
            raise CommandError(
                f"{target} is read-only here, so the backup's secret key and signing seed "
                f"cannot be put back. Run this in a container that mounts the secrets volume "
                f"writable (see the top of restore_backup.py), or pass --skip-secrets "
                f"if this install already holds them.")
        owner = bootsecrets._vigil_owner()
        for name in RESTORED_SECRETS:
            src = secrets / name
            if not src.is_file():
                continue
            tmp = target / f".{name}.restore"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(src.read_text())
            if owner is not None and os.geteuid() == 0:
                os.chown(tmp, *owner)
            os.replace(tmp, target / name)
        self.stdout.write("  secret key and signing seed restored (the database password is kept)")
