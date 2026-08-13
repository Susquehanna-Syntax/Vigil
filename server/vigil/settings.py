import logging
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

# Origins trusted for unsafe (POST) requests. Required when Vigil is served
# behind a reverse proxy or under an external hostname, otherwise Django rejects
# POSTs with "Origin checking failed". Comma-separated, scheme included, e.g.
# DJANGO_CSRF_TRUSTED_ORIGINS=https://vigil.kingdom.local,https://vigil.acme.com
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# Remote access — reverse proxy / tunnel (Cloudflare Tunnel, Tailscale, nginx)
# ---------------------------------------------------------------------------
# Agents are outbound-only, so getting them (and the dashboard) to check in from
# outside the LAN is purely a question of exposing the server at a public or
# overlay address. See docs/REMOTE-ACCESS.md.
#
# VIGIL_PUBLIC_URL is the single knob: the URL Vigil is reached at from outside
# (e.g. https://vigil.example.com or the https://<host>.<tailnet>.ts.net name).
# Its host is appended to ALLOWED_HOSTS and its origin to CSRF_TRUSTED_ORIGINS so
# you don't have to set DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS by
# hand as well.
_public_url = os.environ.get("VIGIL_PUBLIC_URL", "").strip().rstrip("/")

#: Exported so the UI can hand out enrollment commands that point at the
#: external URL rather than whatever address the admin happens to be browsing
#: from — a LAN IP baked into an agent stops working the moment it leaves.
VIGIL_PUBLIC_URL = _public_url

if _public_url:
    from urllib.parse import urlsplit

    _pu = urlsplit(_public_url)
    if _pu.hostname and _pu.hostname not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_pu.hostname)
    if _pu.scheme and _pu.netloc:
        _origin = f"{_pu.scheme}://{_pu.netloc}"
        if _origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(_origin)

# When Vigil sits behind a proxy that terminates TLS (Cloudflare Tunnel, a
# Tailscale HTTPS front, nginx/Caddy), the proxy forwards plain HTTP to Django
# and sets X-Forwarded-Proto/Host. Set VIGIL_TRUST_PROXY=true so Django honors
# those headers — request.is_secure() then returns True, secure cookies work,
# and get_host() reflects the public hostname (needed for the login redirect
# allow-list and absolute URLs).
#
# SECURITY: only enable this when the proxy is the ONLY route to Vigil and always
# rewrites these headers. If the container's port is also reachable directly, a
# client could spoof X-Forwarded-Proto. In the shipped compose the web port is
# published for LAN use, so this stays opt-in, not automatic.
_trust_proxy = os.environ.get("VIGIL_TRUST_PROXY", "false").lower() in ("true", "1", "yes")
if _trust_proxy:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

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
    "apps.baselines",
    "apps.aisuggest",
    "apps.statuspage",
    "apps.automations",
    "apps.reprovision",
    "apps.civilsso",
    # Business features (apps_business/LICENSE) — installed always, unlocked by license
    "apps_business.sites",
    "apps_business.audits",
    "apps_business.branding",
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
# Civil SSO (opt-in shared identity for the SQSY family). Unset = off.
# ---------------------------------------------------------------------------
CIVIL_URL = os.environ.get("CIVIL_URL", "")
CIVIL_APP_SLUG = os.environ.get("CIVIL_APP_SLUG", "vigil")

# AI suggestion call timeout — local BYO endpoints can be slow (cold loads).
VIGIL_AI_TIMEOUT_SECONDS = int(os.environ.get("VIGIL_AI_TIMEOUT_SECONDS", "300"))

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
                "apps.licensing.context_processors.license_banners",
                "apps.civilsso.context_processors.civil",
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

# Extracted OS images for remote reprovisioning (docs/reprovisioning.md §6).
# Kept out of MEDIA_ROOT: these trees are gigabytes, are served to installers
# rather than browsers, and should sit on a volume an operator can size
# independently of the rest of the app's uploads.
VIGIL_IMAGE_ROOT = os.environ.get("VIGIL_IMAGE_ROOT", "/var/lib/vigil/images")

# Where Django spools a multipart upload above FILE_UPLOAD_MAX_MEMORY_SIZE
# (ISO uploads for apps.reprovision.views.image_upload always are — the
# default threshold is 2.5 MB). Left unset, Django spools to the system
# temp dir *inside the container*, not the VIGIL_IMAGE_ROOT volume an
# operator sized for multi-gigabyte images — a single upload could fill the
# container's writable layer before the view ever gets to run. Pointing it
# under VIGIL_IMAGE_ROOT keeps the spool on the same volume as everything
# else this feature writes. Created here, not lazily by a view, because
# Django checks this directory exists the first time a large upload
# arrives, not before — an operator's first real ISO upload is a bad time
# to discover a typo in a bind-mount path. The mkdir is wrapped in a
# try/except because this module also loads in environments (local dev
# without the volume mounted, sandboxed test runners) that cannot write to
# VIGIL_IMAGE_ROOT's default path — settings import must never crash there;
# it just means uploads fail at request time instead, with a clear
# filesystem error, same as any other missing-volume misconfiguration.
FILE_UPLOAD_TEMP_DIR = os.environ.get(
    "VIGIL_UPLOAD_TEMP_DIR", str(Path(VIGIL_IMAGE_ROOT) / "tmp-uploads"))
try:
    Path(FILE_UPLOAD_TEMP_DIR).mkdir(parents=True, exist_ok=True)
except OSError as _upload_dir_exc:
    # VIGIL_IMAGE_ROOT isn't writable in this environment (local dev
    # without the volume mounted, a sandboxed test runner, a hardened
    # deployment running as a non-root user with a restrictively-permissioned
    # volume, ...). Django's own system check (files.E001) fails startup if
    # FILE_UPLOAD_TEMP_DIR is set but does not exist, so falling back to its
    # default of None (system temp dir) — the behaviour before this setting
    # existed — keeps the process starting at all. But that fallback is
    # exactly the failure mode this setting exists to prevent (multi-
    # gigabyte uploads spooling onto the container's writable layer instead
    # of the sized volume), so it must not happen silently: a warning here
    # is the only signal an operator gets, and without it the next person
    # debugs a full disk instead of the permissions error that caused it.
    logging.getLogger("vigil.reprovision").warning(
        "FILE_UPLOAD_TEMP_DIR %r is not writable (%s) — falling back to the "
        "system temp dir. Large ISO uploads will spool inside the "
        "container instead of onto the VIGIL_IMAGE_ROOT volume. Fix the "
        "permissions on %r or set VIGIL_UPLOAD_TEMP_DIR to a writable path.",
        FILE_UPLOAD_TEMP_DIR, _upload_dir_exc, FILE_UPLOAD_TEMP_DIR,
    )
    FILE_UPLOAD_TEMP_DIR = None

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
# Request body size
# ---------------------------------------------------------------------------
# Django's default is 2.5 MB, and it is enforced in HttpRequest.body — before
# any view runs. A body over the limit raises RequestDataTooBig and the whole
# POST fails with a bare 400 that no Vigil code can annotate, leaving the task
# DISPATCHED forever with nothing anywhere to explain it.
#
# Task results are the large payload: a Trivy scan report. The agent
# deduplicates and gzips those before sending, so even a host with thousands of
# findings arrives well under a megabyte, and it caps task output at 2,000,000
# characters (vigil_agent.client._MAX_OUTPUT).
#
# This clears that cap several times over. It is deliberately not sized tight:
# exceeding it fails in HttpRequest.body, before any view runs, as a bare 400
# that no Vigil code can annotate — the task would sit DISPATCHED forever with
# nothing anywhere to explain it. Cheap headroom buys a failure mode we never
# have to diagnose.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get("VIGIL_MAX_REQUEST_BODY_BYTES", 8 * 1024 * 1024))

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
    "reprovision-deadline-sweep": {
        "task": "reprovision.sweep_deadlines",
        "schedule": 60.0,  # every 60s — a stuck rebuild should surface fast
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
    "sample-host-uptime": {
        "task": "statuspage.sample_uptime",
        "schedule": 300.0,  # every 5 minutes — feeds the status-page uptime bars
    },
    "prune-old-uptime-samples": {
        "task": "statuspage.prune_old_uptime_samples",
        "schedule": 86400.0,  # once daily
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

# Server build version — surfaced on the About page and the /api/v1/about/
# endpoint. Bump this on every release; the Git tag (v2026.2.3, etc.) and
# this constant should stay in lockstep.
VIGIL_VERSION = "2026.8.2"

# ---------------------------------------------------------------------------
# Agent distribution — filesystem path where compiled binaries live.
# In the Docker image this is pre-populated by the multi-stage build.
# ---------------------------------------------------------------------------
VIGIL_AGENT_DIST_DIR = Path(os.environ.get("VIGIL_AGENT_DIST_DIR", str(BASE_DIR / "agent_dist")))


def _detect_agent_version() -> str:
    """Work out which agent version this build actually ships.

    This used to be ``os.environ.get("VIGIL_AGENT_VERSION", <a literal>)``, and
    every release had to remember to bump the literal here, the default in
    docker-compose.yml, and whatever each operator had in .env. Nobody did, so
    the three drifted — compose said 2026.3.8, settings said 2026.3.16, the
    wiki said 2026.2.2 — and because compose always passes the variable, its
    stale default silently won everywhere. check_outdated_agents compares each
    host against this value, so a wrong answer either alerts a whole fleet
    that is perfectly current or stays quiet while every host runs an agent
    too old to do its job.

    So it is detected rather than configured, most specific source first:

      1. ``VERSION`` beside the bundled binaries, stamped from the agent
         source at image build time — authoritative in Docker, where the
         agent source itself is not present;
      2. the agent source, for a checkout where ``server/`` and ``agent/``
         sit side by side (development, and the repo layout itself);
      3. the server's own version, since both ship from this one repo.

    Never raises: a version string is not worth failing startup over.
    """
    stamped = VIGIL_AGENT_DIST_DIR / "VERSION"
    try:
        version = stamped.read_text(encoding="utf-8").strip()
        if version:
            return version
    except OSError:
        pass

    source = BASE_DIR.parent / "agent" / "vigil_agent" / "__version__.py"
    try:
        import re as _re

        match = _re.search(r"""__version__\s*=\s*["']([^"']+)["']""",
                           source.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    except OSError:
        pass

    return VIGIL_VERSION


VIGIL_AGENT_VERSION = _detect_agent_version()

#: True when an operator still has VIGIL_AGENT_VERSION set. It is deliberately
#: ignored now — honouring it is what let a stale compose default pin the whole
#: fleet to a version that shipped months ago — but silently ignoring a
#: variable someone set on purpose is its own trap, so startup says so once.
VIGIL_AGENT_VERSION_ENV_IGNORED = bool(os.environ.get("VIGIL_AGENT_VERSION"))

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
