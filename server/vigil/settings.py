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
    "apps.licensing",
    # Business features (apps_business/LICENSE) — installed always, unlocked by license
    "apps_business.sites",
    "apps_business.audits",
]

# ---------------------------------------------------------------------------
# Licensing (SQSY-LICENSING.md)
# ---------------------------------------------------------------------------
# Base64 Ed25519 public key that license blobs must verify against. Dev and
# prod keys never mix (SQSY-LICENSING.md §7a): the DEV key (pairs with
# Mercantil's gitignored dev-signing.key) is the default only when DEBUG, so
# local development can mint test licenses out of the box. Everywhere else the
# operator sets VIGIL_LICENSE_PUBLIC_KEY — to the production key once it is
# born in KMS, or explicitly to the dev key on internal dogfood installs. An
# empty key simply means no license verifies: free tier, monitoring untouched.
_DEV_LICENSE_PUBLIC_KEY = "HDIgm72yEkIopWgvsv0Q6Gp695l4ecOZMYnP2by7+IQ="
VIGIL_LICENSE_PUBLIC_KEY = os.environ.get(
    "VIGIL_LICENSE_PUBLIC_KEY", _DEV_LICENSE_PUBLIC_KEY if DEBUG else ""
)

# ---------------------------------------------------------------------------
# Extra apps (operator extensions)
# ---------------------------------------------------------------------------
# Community extensions are Django apps placed on the PYTHONPATH and named here
# via the VIGIL_EXTRA_APPS env var, comma-separated:
#
#     VIGIL_EXTRA_APPS=my_extension.dashboards
#
# Each extra app may register features (vigil.editions), subscribe to events
# (vigil.hooks), and expose a urls.py (auto-mounted in vigil/urls.py). Core
# never imports extension code. See docs/pro-extension-points.md for the
# contract. (Business features no longer load this way — they ship in this
# repo under apps_business/ and are unlocked by the license.)
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
# Session / CSRF cookie hardening
# ---------------------------------------------------------------------------
# The console session lives exclusively in an HttpOnly cookie — page JS can
# never read it and nothing auth-related is ever placed in localStorage
# (localStorage holds only UI preferences: pinned hosts, table columns).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
# Self-hosted Vigil frequently runs plain HTTP on a trusted LAN, so Secure
# flags are opt-in. Set VIGIL_SECURE_COOKIES=true when serving over HTTPS
# (directly or behind a TLS-terminating proxy).
_secure_cookies = os.environ.get("VIGIL_SECURE_COOKIES", "false").lower() in ("true", "1", "yes")
SESSION_COOKIE_SECURE = _secure_cookies
CSRF_COOKIE_SECURE = _secure_cookies

# ---------------------------------------------------------------------------
# Task signing — Ed25519
# Generate with: python -c "import base64; from nacl.signing import SigningKey; print(base64.b64encode(bytes(SigningKey.generate())).decode())"
# ---------------------------------------------------------------------------
VIGIL_SIGNING_KEY_SEED = os.environ.get("VIGIL_SIGNING_KEY_SEED", "")

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # Session-cookie auth only. TokenAuthentication was listed historically
    # but rest_framework.authtoken was never installed, so any request
    # carrying an "Authorization: Token …" header would 500 — and Vigil's
    # agents use their own Bearer scheme (apps.hosts.authentication), not
    # DRF tokens.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
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
    "check-db-disk-usage": {
        "task": "metrics.check_db_disk_usage",
        "schedule": 3600.0,  # every hour — storage safety valve (self-monitoring)
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
# Metric retention & TimescaleDB storage policies
# ---------------------------------------------------------------------------
# Raw metric retention horizon. On TimescaleDB this drives the native
# drop_chunks retention policy (migration metrics/0002); on SQLite/plain
# Postgres it drives the DELETE-based fallback prune task. Downsampled 1-hour
# and 1-day continuous-aggregate rollups are retained far longer
# (VIGIL_TS_HOURLY_RETENTION / VIGIL_TS_DAILY_RETENTION) so trend history
# survives raw expiry. Chunks older than VIGIL_TS_COMPRESS_AFTER are compressed
# (~10-20x). See docs/timescaledb-storage.md.
VIGIL_METRIC_RETENTION_DAYS = int(os.environ.get("VIGIL_METRIC_RETENTION_DAYS", "30"))

# Storage safety valve — metrics.check_db_disk_usage logs WARNING/ERROR when the
# database trends toward the disk limit. Set to 0 to disable a threshold.
VIGIL_DB_SIZE_WARN_GB = float(os.environ.get("VIGIL_DB_SIZE_WARN_GB", "20"))
VIGIL_DB_SIZE_CRIT_GB = float(os.environ.get("VIGIL_DB_SIZE_CRIT_GB", "40"))

# ---------------------------------------------------------------------------
# Agent distribution — filesystem path where compiled binaries live.
# In the Docker image this is pre-populated by the multi-stage build.
# ---------------------------------------------------------------------------
VIGIL_AGENT_DIST_DIR = Path(os.environ.get("VIGIL_AGENT_DIST_DIR", str(BASE_DIR / "agent_dist")))
VIGIL_AGENT_VERSION = os.environ.get("VIGIL_AGENT_VERSION", "2026.3.16")

# Server build version — surfaced on the About page and the /api/v1/about/
# endpoint. Bump this on every release; the Git tag (v2026.2.3, etc.) and
# this constant should stay in lockstep.
VIGIL_VERSION = "2026.3.16"

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
# Port list UUID for scan targets. Empty falls back to the well-known
# "All IANA assigned TCP" list; gvmd 20.8+ rejects targets with no port list.
GREENBONE_PORT_LIST_ID = os.environ.get("GREENBONE_PORT_LIST_ID", "")

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
