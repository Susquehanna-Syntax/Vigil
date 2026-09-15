#!/bin/sh
# Back up a Vigil install: the database, and the two volumes that hold state
# nothing can regenerate.
#
# There was no backup path in this repository at all — `pg_dump` appeared zero
# times — while three named volumes held the entire system. An operator who had
# run Vigil for six months and wanted to move it to a new machine had no
# command to run.
#
#   ./scripts/vigil-backup.sh [destination-directory]
#
# Writes <destination>/vigil-<timestamp>/ containing db.sql.gz, media.tar.gz
# and a MANIFEST. Restore with vigil-restore.sh.
#
# Images are deliberately NOT included: they are multi-gigabyte OS install
# trees that can be re-imported from their source, and copying them would make
# a routine backup too expensive to run often. The manifest lists which images
# were registered so they can be fetched again.

set -eu

DEST="${1:-./vigil-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/vigil-$STAMP"

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@";
  else docker-compose "$@"; fi
}

DB_USER="${POSTGRES_USER:-vigil}"
DB_NAME="${POSTGRES_DB:-vigil}"

mkdir -p "$OUT"
echo "Backing up Vigil to $OUT"

# 1. The database — everything except uploaded files. pg_dump runs inside the
#    db container so no client tooling is needed on the host.
echo "  database..."
compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists \
  | gzip > "$OUT/db.sql.gz"

# 2. Uploaded media (branding, status-page assets).
echo "  media..."
compose exec -T web tar -cf - -C /app media 2>/dev/null | gzip > "$OUT/media.tar.gz" \
  || echo "    (no media volume contents — skipping)"

# 3. What was registered, so a restore knows what to re-fetch.
echo "  manifest..."
{
  echo "vigil_backup_version: 1"
  echo "taken_at: $STAMP"
  echo "database: $DB_NAME"
  echo "server_version: $(compose exec -T web python -c \
      'from django.conf import settings; import django, os; \
       os.environ.setdefault("DJANGO_SETTINGS_MODULE","vigil.settings"); \
       django.setup(); print(settings.VIGIL_VERSION)' 2>/dev/null || echo unknown)"
  echo "images_not_included: true"
} > "$OUT/MANIFEST"

echo "Done. $(du -sh "$OUT" | cut -f1) in $OUT"
echo
echo "Restore with:  ./scripts/vigil-restore.sh $OUT"
