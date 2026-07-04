import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "insecure-dev-key-change-me-in-production",
)

# DEBUG defaults to false so a forgotten env var doesn't ship verbose error
# pages (or — historically — silently disable 2FA) to production. Local dev
# should use ``settings_local.py`` or explicitly set DJANGO_DEBUG=true.
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "django_celery_beat",
    # Vigil apps
    "apps.hosts",
    "apps.metrics",
    "apps.alerts",
    "apps.tasks",
    "apps.vulns",
    "apps.accounts",
    "apps.agent_dist",
]

# ---------------------------------------------------------------------------
# Edition extension apps (Pro / Enterprise)
# ---------------------------------------------------------------------------
# Community core is self-contained. The commercial editions ship as separate
# repos (Vigil-Pro, Vigil-Enterprise) whose Django apps are placed on the
# PYTHONPATH and named here via the VIGIL_EXTRA_APPS env var, comma-separated:
#
#     VIGIL_EXTRA_APPS=vigil_pro.rbac,vigil_pro.baselines
#
# Each extra app may register features (vigil.editions), subscribe to events
# (vigil.hooks), and expose a urls.py (auto-mounted in vigil/urls.py). Core
# never imports edition code. See docs/pro-extension-points.md for the contract.
VIGIL_EXTRA_APPS = [
    a.strip() for a in os.environ.get("VIGIL_EXTRA_APPS", "").split(",") if a.strip()
]
INSTALLED_APPS += VIGIL_EXTRA_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.SetupRedirectMiddleware",
]

ROOT_URLCONF = "vigil.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vigil.wsgi.application"

# ---------------------------------------------------------------------------
# Database — PostgreSQL + TimescaleDB (SQLite fallback for local dev)
# ---------------------------------------------------------------------------
_use_sqlite = os.environ.get("USE_SQLITE", "").lower() in ("true", "1", "yes")
if _use_sqlite:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "vigil"),
            "USER": os.environ.get("POSTGRES_USER", "vigil"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "vigil"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# i18n / tz
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---------------------------------------------------------------------------
# Media files — agent binaries and other uploaded files
# ---------------------------------------------------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
LOGIN_URL = "/login/"

# ---------------------------------------------------------------------------
# Task signing — Ed25519
# Generate with: python -c "import base64; from nacl.signing import SigningKey; print(base64.b64encode(bytes(SigningKey.generate())).decode())"
# ---------------------------------------------------------------------------
VIGIL_SIGNING_KEY_SEED = os.environ.get("VIGIL_SIGNING_KEY_SEED", "")

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# ---------------------------------------------------------------------------
# Celery — Redis broker
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# Vigil's beat tasks are fire-and-forget — don't accumulate results in Redis.
CELERY_TASK_IGNORE_RESULT = True
CELERY_BEAT_SCHEDULE = {
    "evaluate-alert-rules": {
        "task": "alerts.evaluate_alert_rules",
        "schedule": 60.0,  # every 60 seconds
    },
    "expire-alert-acknowledgements": {
        "task": "alerts.expire_acknowledgements",
        "schedule": 60.0,  # every 60 seconds — re-fire lapsed timed acks promptly
    },
    "mark-stale-hosts-offline": {
        "task": "alerts.mark_stale_hosts_offline",
        "schedule": 120.0,  # every 2 minutes
    },
    "prune-old-metric-points": {
        "task": "metrics.prune_old_metric_points",
        "schedule": 3600.0,  # every hour
    },
    "sync-vulns": {
        "task": "vulns.sync_vulns",
        "schedule": 3600.0,  # every hour — iterates every configured scanner
    },
    "snapshot-vuln-scores": {
        "task": "vulns.snapshot_scores",
        "schedule": 86400.0,  # once daily — powers score sparklines + trend
    },
    "check-docker-image-updates": {
        "task": "alerts.check_docker_image_updates",
        "schedule": 600.0,  # every 10 minutes
    },
    "check-outdated-agents": {
        "task": "alerts.check_outdated_agents",
        "schedule": 3600.0,  # every hour
    },
    "expire-stale-tasks": {
        "task": "tasks.expire_stale_tasks",
        "schedule": 600.0,  # every 10 minutes — sweep wedged DISPATCHED tasks
    },
}

# How long past its own TTL a dispatched task may stay silent before the
# expiry sweep marks it EXPIRED. Generous on purpose: TTL bounds when an
# agent may START a task, not how long it may run (a filesystem-wide
# Trivy scan legitimately takes minutes).
VIGIL_TASK_EXPIRY_GRACE_SECONDS = int(
    os.environ.get("VIGIL_TASK_EXPIRY_GRACE_SECONDS", "3600")
)

# ---------------------------------------------------------------------------
# Metric retention
# ---------------------------------------------------------------------------
VIGIL_METRIC_RETENTION_DAYS = int(os.environ.get("VIGIL_METRIC_RETENTION_DAYS", "30"))

# ---------------------------------------------------------------------------
# Agent distribution — filesystem path where compiled binaries live.
# In the Docker image this is pre-populated by the multi-stage build.
# ---------------------------------------------------------------------------
VIGIL_AGENT_DIST_DIR = Path(os.environ.get("VIGIL_AGENT_DIST_DIR", str(BASE_DIR / "agent_dist")))
VIGIL_AGENT_VERSION = os.environ.get("VIGIL_AGENT_VERSION", "2026.3.4")

# Server build version — surfaced on the About page and the /api/v1/about/
# endpoint. Bump this on every release; the Git tag (v2026.2.3, etc.) and
# this constant should stay in lockstep.
VIGIL_VERSION = "2026.3.4"

# ---------------------------------------------------------------------------
# Display / locale
# ---------------------------------------------------------------------------
VIGIL_TIMEZONE = os.environ.get("VIGIL_TIMEZONE", "UTC")
VIGIL_TIME_FORMAT = os.environ.get("VIGIL_TIME_FORMAT", "12h")  # "12h" or "24h"

# ---------------------------------------------------------------------------
# Nessus / Tenable vulnerability integration
# ---------------------------------------------------------------------------
NESSUS_URL = os.environ.get("NESSUS_URL", "")
NESSUS_ACCESS_KEY = os.environ.get("NESSUS_ACCESS_KEY", "")
NESSUS_SECRET_KEY = os.environ.get("NESSUS_SECRET_KEY", "")
NESSUS_VERIFY_SSL = os.environ.get("NESSUS_VERIFY_SSL", "true").lower() in ("true", "1")

# ---------------------------------------------------------------------------
# Greenbone / OpenVAS vulnerability integration (BYO container)
# ---------------------------------------------------------------------------
# Talks GMP (XML over TLS) to a Greenbone Community Edition stack the
# user runs themselves. URL is host:port of the GMP listener (default
# 9390 for greenbone-community-container).
GREENBONE_URL = os.environ.get("GREENBONE_URL", "")
GREENBONE_USERNAME = os.environ.get("GREENBONE_USERNAME", "")
GREENBONE_PASSWORD = os.environ.get("GREENBONE_PASSWORD", "")
GREENBONE_VERIFY_SSL = os.environ.get("GREENBONE_VERIFY_SSL", "true").lower() in ("true", "1")

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
VIGIL_NOTIFICATION_FROM_EMAIL = os.environ.get("VIGIL_NOTIFICATION_FROM_EMAIL", "vigil@localhost")
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() in ("true", "1")
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
    # Suppress chatty third-party loggers that otherwise flood syslog
    # via journald when the server runs as a systemd service.
    "loggers": {
        "urllib3": {"level": "WARNING", "propagate": True},
        "requests": {"level": "WARNING", "propagate": True},
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "django_celery_beat": {"level": "WARNING", "propagate": True},
        "celery.beat": {"level": "WARNING", "propagate": True},
        "celery.worker": {"level": "WARNING", "propagate": True},
    },
}
