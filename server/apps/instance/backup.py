"""The nightly backup (sprint M3 phase 4).

One archive per run in VIGIL_BACKUP_DIR (a volume): the database as a
pg_dump custom-format file, the uploaded media, and the generated secrets
(without them a restored install cannot sign tasks its agents accept, nor
decrypt the credentials it stores). The worker runs it from an hourly beat
tick; Settings → Retention & Limits turns it on and picks the hour and how
many to keep, and lists each run (BackupRun).

The archive holds every secret the install has, so it is written 0600 into
a directory only the worker's user can read, and an off-host copy is the
operator's to protect (an encrypted remote target is phase 04c).

Restore with ``manage.py restore_backup <archive>``.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^vigil-\d{8}T\d{6}Z\.tar\.gz$")
FORMAT_VERSION = 1
PG_DUMP_TIMEOUT = 3 * 3600


def backup_dir() -> Path:
    return Path(getattr(settings, "VIGIL_BACKUP_DIR", "/var/lib/vigil/backups"))


def _setting(name):
    from .config import setting
    return setting(name)


def due(now=None) -> bool:
    """On, this is the configured hour (server time zone), and no run has
    succeeded or is running since the start of that hour today."""
    from .models import BackupRun

    if not _setting("VIGIL_BACKUP_ENABLED"):
        return False
    now = timezone.localtime(now or timezone.now())
    hour = _setting("VIGIL_BACKUP_HOUR")
    if not isinstance(hour, int) or not 0 <= hour <= 23 or now.hour != hour:
        return False
    since = now.replace(minute=0, second=0, microsecond=0)
    recent = BackupRun.objects.filter(started_at__gte=since).exclude(state=BackupRun.State.FAILED)
    # A run stuck in RUNNING for half a day (the worker died) does not block.
    stale = now - timedelta(hours=12)
    return not recent.filter(started_at__gte=stale).exists()


def _dump_database(into: Path) -> str:
    """Write the database to `into`; return its name inside the archive."""
    db = settings.DATABASES["default"]
    if db["ENGINE"].endswith("sqlite3"):
        # Development and tests: a consistent copy through SQLite's own API.
        import sqlite3
        src = sqlite3.connect(str(db["NAME"]))
        dst = sqlite3.connect(str(into))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return "db.sqlite3"
    env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--file", str(into),
           "--host", str(db.get("HOST") or "localhost"), "--port", str(db.get("PORT") or 5432),
           "--username", str(db["USER"]), str(db["NAME"])]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=PG_DUMP_TIMEOUT)
    if done.returncode != 0:
        raise RuntimeError(f"pg_dump failed ({done.returncode}): {done.stderr.strip()[-1500:]}")
    return "db.dump"


def _add_tree(tar: tarfile.TarFile, root: Path, arcname: str) -> bool:
    if not root.is_dir():
        return False
    tar.add(str(root), arcname=arcname, recursive=True)
    return True


def run_backup(now=None):
    """Make one archive and record it; prune old ones. Returns the BackupRun."""
    from vigil import bootsecrets

    from .models import BackupRun

    started = now or timezone.now()
    name = f"vigil-{started.astimezone(dt_timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz"
    run = BackupRun.objects.create(started_at=started, name=name)
    out_dir = backup_dir()
    work = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir, 0o700)
        work = Path(tempfile.mkdtemp(prefix=".partial-", dir=str(out_dir)))
        db_file = work / "db"
        db_name = _dump_database(db_file)

        tmp_archive = work / name
        fd = os.open(tmp_archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            tar.add(str(db_file), arcname=db_name)
            contents = [db_name]
            if _add_tree(tar, Path(settings.MEDIA_ROOT), "media"):
                contents.append("media")
            # By name: the secrets directory is execute-only (0711), so it
            # cannot be listed, only opened at the names bootsecrets uses.
            added = 0
            for secret in bootsecrets.NAMES:
                path = Path(bootsecrets.secrets_dir()) / secret
                if path.is_file():
                    tar.add(str(path), arcname=f"secrets/{secret}")
                    added += 1
            if added:
                contents.append("secrets")
            manifest = json.dumps({
                "format": FORMAT_VERSION,
                "vigil_version": settings.VIGIL_VERSION,
                "taken_at": started.isoformat(),
                "database": db_name,
                "contents": contents,
            }, indent=1).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(manifest)
            info.mtime = int(started.timestamp())
            tar.addfile(info, io.BytesIO(manifest))
        final = out_dir / name
        os.replace(tmp_archive, final)
        run.state = BackupRun.State.OK
        run.size_bytes = final.stat().st_size
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
        logger.exception("backup %s failed", name)
        run.state = BackupRun.State.FAILED
        run.error = str(exc)[:5000]
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        run.finished_at = timezone.now()
        run.save()
    if run.state == BackupRun.State.OK:
        prune(_setting("VIGIL_BACKUP_KEEP"))
        _copy_offsite(run)
    return run


def _copy_offsite(run):
    """The sealed off-site copy, when one is configured. A failure here is
    recorded beside the run, which still succeeded: the archive on this
    machine is intact."""
    from . import offsite

    if not offsite.configured():
        return
    try:
        offsite.copy_offsite(backup_dir() / run.name, _setting("VIGIL_BACKUP_KEEP"))
        run.offsite = "copied"
    except Exception as exc:  # noqa: BLE001 - recorded, and the page shows it
        logger.exception("off-site copy of %s failed", run.name)
        run.offsite = "failed"
        run.offsite_error = str(exc)[:5000]
    run.save(update_fields=["offsite", "offsite_error"])


def prune(keep) -> list[str]:
    """Delete all but the newest `keep` archives this module made. Never
    touches a file whose name it did not choose."""
    if not isinstance(keep, int) or keep < 1:
        return []
    names = sorted((p.name for p in backup_dir().iterdir() if NAME_RE.match(p.name)), reverse=True)
    gone = names[keep:]
    for n in gone:
        (backup_dir() / n).unlink(missing_ok=True)
    return gone
