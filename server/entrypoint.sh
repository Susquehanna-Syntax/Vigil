#!/bin/sh
set -e

echo "Waiting for PostgreSQL..."
while ! python -c "
import socket, os
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((os.environ.get('POSTGRES_HOST', 'db'), int(os.environ.get('POSTGRES_PORT', 5432))))
s.close()
" 2>/dev/null; do
    sleep 1
done
echo "PostgreSQL is up."

echo "Running migrations..."
python manage.py migrate --noinput

# Drop privileges for the actual work. Everything above needs root only
# because the mounted volumes arrive owned by root on an install that has
# been upgraded from a version that ran as root throughout.
#
# VIGIL_RUN_AS_ROOT=1 keeps the old behaviour for anyone whose setup depends
# on it. It should not be needed, and if it is, that is worth reporting.
if [ "${VIGIL_RUN_AS_ROOT:-0}" = "1" ] || [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

if command -v gosu >/dev/null 2>&1; then
    # Only the paths the app actually writes. Chowning /app wholesale would
    # rewrite the entire image layer on every boot for no reason.
    for path in /app/media /var/lib/vigil/images /app/staticfiles; do
        [ -d "$path" ] && chown -R vigil:vigil "$path" 2>/dev/null || true
    done
    echo "Dropping privileges to 'vigil'."
    exec gosu vigil "$@"
fi

echo "WARNING: gosu is unavailable — continuing as root." >&2
exec "$@"
