#!/bin/sh
# Restore a Vigil backup taken by vigil-backup.sh.
#
#   ./scripts/vigil-restore.sh <backup-directory>
#
# This REPLACES the current database. It stops the workers first so nothing is
# writing while the restore runs, and starts them again afterwards.

set -eu

SRC="${1:?usage: vigil-restore.sh <backup-directory>}"
[ -f "$SRC/db.sql.gz" ] || { echo "No db.sql.gz in $SRC" >&2; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@";
  else docker-compose "$@"; fi
}

DB_USER="${POSTGRES_USER:-vigil}"
DB_NAME="${POSTGRES_DB:-vigil}"

echo "This will REPLACE the contents of database '$DB_NAME'."
printf "Type the database name to confirm: "
read -r CONFIRM
[ "$CONFIRM" = "$DB_NAME" ] || { echo "Aborted."; exit 1; }

echo "Stopping workers so nothing writes during the restore..."
compose stop celery-worker celery-beat web

echo "Restoring database..."
gunzip -c "$SRC/db.sql.gz" | compose exec -T db psql -U "$DB_USER" -d "$DB_NAME"

if [ -f "$SRC/media.tar.gz" ]; then
  echo "Restoring media..."
  compose start web >/dev/null
  gunzip -c "$SRC/media.tar.gz" | compose exec -T web tar -xf - -C /app
fi

echo "Starting everything again..."
compose up -d

echo "Done. Migrations run on start, so a backup from an older Vigil is"
echo "brought up to the current schema automatically."
