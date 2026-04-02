"""Local dev settings — SQLite, no Redis/Celery dependencies.

Usage: python manage.py runserver --settings=vigil.settings_local
"""

from .settings import *  # noqa: F401, F403

DEBUG = True

# SQLite instead of PostgreSQL
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Celery runs tasks synchronously in-process (no Redis needed)
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Don't need celery-beat scheduler with SQLite
CELERY_BEAT_SCHEDULER = "celery.beat:PersistentScheduler"
