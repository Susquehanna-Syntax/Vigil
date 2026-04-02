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

exec "$@"
