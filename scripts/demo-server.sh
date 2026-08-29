#!/usr/bin/env bash
# Start the LAN demo instance on :8500 against the local SQLite database.
#
# Reproducible on purpose: the previous demo was started by hand, ran with
# --noreload for two days, and was therefore still executing the code from the
# day it launched while the checkout moved on. If you change Python, restart
# through this script.
set -euo pipefail
cd "$(dirname "$0")/../server"
set -a; . ../.env; set +a
export USE_SQLITE=1
export DJANGO_ALLOWED_HOSTS="10.0.0.53,127.0.0.1,localhost"
export DJANGO_CSRF_TRUSTED_ORIGINS="http://10.0.0.53:8500"
exec .venv/bin/python manage.py runserver 0.0.0.0:8500 --noreload --insecure
