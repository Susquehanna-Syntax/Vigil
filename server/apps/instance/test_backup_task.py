"""The nightly backup and its restore (sprint M3 phase 4b).

The database dump itself (pg_dump / pg_restore against TimescaleDB) is
proven in CI by the restore job; here the dump is a stand-in file and every
other rule is checked: when it runs, what the archive holds and its mode,
pruning, failure, and what a restore puts back (never the database password,
never a path outside its own tree).
"""
import io
import json
import os
import tarfile
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.instance import backup
from apps.instance.models import BackupRun


def fake_dump(into: Path):
    into.write_bytes(b"pretend database")
    return "db.sqlite3"


class _Dirs(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.out, self.media, self.secrets = root / "backups", root / "media", root / "secrets"
        (self.media / "branding").mkdir(parents=True)
        (self.media / "branding" / "logo.png").write_bytes(b"png")
        self.secrets.mkdir()
        for name, value in (("django_secret_key", "old-secret"), ("signing_key_seed", "old-seed"),
                            ("postgres_password", "old-db-pass")):
            (self.secrets / name).write_text(value + "\n")
        os.chmod(self.secrets, 0o711)
        for p in (override_settings(VIGIL_BACKUP_DIR=str(self.out), MEDIA_ROOT=str(self.media)),
                  mock.patch.dict(os.environ, {"VIGIL_SECRETS_DIR": str(self.secrets),
                                               "VIGIL_BACKUP_ENABLED": "", "VIGIL_BACKUP_HOUR": "",
                                               "VIGIL_BACKUP_KEEP": ""}),
                  mock.patch.object(backup, "_dump_database", side_effect=fake_dump)):
            p.start() if hasattr(p, "start") else p.enable()
            self.addCleanup(p.stop if hasattr(p, "stop") else p.disable)

    def settings_(self, **values):
        from apps.instance import config
        names = {"enabled": "VIGIL_BACKUP_ENABLED", "hour": "VIGIL_BACKUP_HOUR", "keep": "VIGIL_BACKUP_KEEP"}
        real = config.setting
        patched = {names[k]: v for k, v in values.items()}
        return mock.patch.object(config, "setting", side_effect=lambda n: patched.get(n, real(n)))


class DueTests(_Dirs):
    def at(self, hour):
        return timezone.localtime().replace(hour=hour, minute=10, second=0, microsecond=0)

    def test_off_by_default(self):
        self.assertFalse(backup.due(self.at(2)))

    def test_only_in_the_configured_hour_and_once(self):
        with self.settings_(enabled=True, hour=2):
            self.assertFalse(backup.due(self.at(3)))
            self.assertTrue(backup.due(self.at(2)))
            BackupRun.objects.create(started_at=self.at(2) - timedelta(minutes=5), state="ok")
            self.assertFalse(backup.due(self.at(2)))

    def test_a_failed_run_is_retried_next_tick(self):
        with self.settings_(enabled=True, hour=2):
            BackupRun.objects.create(started_at=self.at(2) - timedelta(minutes=5), state="failed")
            self.assertTrue(backup.due(self.at(2)))


class RunTests(_Dirs):
    def test_an_archive_holds_db_media_secrets_and_a_manifest(self):
        run = backup.run_backup()
        self.assertEqual(run.state, "ok", run.error)
        archive = self.out / run.name
        self.assertEqual(oct(archive.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o700")
        self.assertEqual(run.size_bytes, archive.stat().st_size)
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
            manifest = json.load(tar.extractfile("MANIFEST.json"))
        self.assertTrue({"db.sqlite3", "media/branding/logo.png", "secrets/signing_key_seed",
                         "secrets/django_secret_key", "MANIFEST.json"} <= names, names)
        self.assertEqual(manifest["contents"], ["db.sqlite3", "media", "secrets"])
        self.assertFalse([p for p in self.out.iterdir() if p.name.startswith(".partial")])

    def test_a_failure_is_recorded_and_leaves_nothing_behind(self):
        with mock.patch.object(backup, "_dump_database", side_effect=RuntimeError("pg_dump failed (1): no")):
            run = backup.run_backup()
        self.assertEqual(run.state, "failed")
        self.assertIn("pg_dump failed", run.error)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_pruning_keeps_the_newest_and_only_touches_its_own_files(self):
        self.out.mkdir(mode=0o700)
        (self.out / "notes.txt").write_text("mine")
        start = timezone.now() - timedelta(days=10)
        with self.settings_(keep=2):
            for day in range(4):
                backup.run_backup(now=start + timedelta(days=day))
        left = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len([n for n in left if backup.NAME_RE.match(n)]), 2)
        self.assertIn("notes.txt", left)
        self.assertIn(f"vigil-{(start + timedelta(days=3)).astimezone(backup.dt_timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz",
                      left)


class RestoreTests(_Dirs):
    def archive(self):
        return self.out / backup.run_backup().name

    def restore(self, path, *extra):
        with mock.patch("apps.instance.management.commands.restore_backup.Command._restore_database"):
            call_command("restore_backup", str(path), "--yes", *extra, stdout=io.StringIO())

    def test_restores_media_and_keys_but_not_the_database_password(self):
        path = self.archive()
        # This install has since generated its own secrets and lost the logo.
        for name, value in (("django_secret_key", "new-secret"), ("signing_key_seed", "new-seed"),
                            ("postgres_password", "new-db-pass")):
            (self.secrets / name).write_text(value + "\n")
        (self.media / "branding" / "logo.png").unlink()
        self.restore(path)
        self.assertEqual((self.secrets / "django_secret_key").read_text().strip(), "old-secret")
        self.assertEqual((self.secrets / "signing_key_seed").read_text().strip(), "old-seed")
        self.assertEqual((self.secrets / "postgres_password").read_text().strip(), "new-db-pass")
        self.assertEqual(oct((self.secrets / "signing_key_seed").stat().st_mode & 0o777), "0o600")
        self.assertTrue((self.media / "branding" / "logo.png").exists())

    def test_skip_secrets(self):
        path = self.archive()
        (self.secrets / "signing_key_seed").write_text("new-seed\n")
        self.restore(path, "--skip-secrets")
        self.assertEqual((self.secrets / "signing_key_seed").read_text().strip(), "new-seed")

    def test_a_read_only_secrets_volume_is_refused_with_directions(self):
        path = self.archive()
        with mock.patch("os.access", return_value=False), self.assertRaisesMessage(CommandError, "writable"):
            self.restore(path)

    def test_an_archive_cannot_write_outside_its_tree(self):
        # The restore unpacks into a fresh directory under the temp dir, so
        # "../<name>" would land in the temp dir itself if it were allowed.
        import secrets as _secrets
        name = f"vigil-escape-{_secrets.token_hex(4)}"
        target = Path(tempfile.gettempdir()) / name
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        evil = self.out / "evil.tar.gz"
        self.out.mkdir(exist_ok=True)
        with tarfile.open(evil, "w:gz") as tar:
            data = b"owned"
            info = tarfile.TarInfo(f"../{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(tarfile.FilterError):
            self.restore(evil)
        self.assertFalse(target.exists())

    def test_an_unknown_format_is_refused(self):
        odd = self.out / "odd.tar.gz"
        self.out.mkdir(exist_ok=True)
        with tarfile.open(odd, "w:gz") as tar:
            data = json.dumps({"format": 99}).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesMessage(CommandError, "format"):
            self.restore(odd)
