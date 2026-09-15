"""Resolve a setting from the environment, the database, or the code default.

    from apps.instance.config import setting
    url = setting("GREENBONE_URL")

Precedence, highest first:

1. **The environment.** If the variable is set to a non-empty value, it wins
   and the UI shows the field read-only. A GitOps install whose compose file
   is the source of truth must never be silently overridden by a row someone
   typed into a form — the same rule CivilConfig already follows.
2. **An ``InstanceSetting`` row**, written from the Settings page.
3. **``django.conf.settings``**, which is the code default when no environment
   variable was set.

Reads are served from a small process-local cache. It is refreshed when this
process writes a row (via the post-save signal) and otherwise expires after a
few seconds, which is what keeps a Celery worker from serving a stale scanner
password indefinitely after the web process saved a new one. Bounded staleness
is the trade deliberately taken here: the alternative is a database round-trip
on every request for the timezone.
"""

from __future__ import annotations

import os
import time
import zoneinfo

from django.conf import settings as django_settings
from django.core.mail import get_connection

from apps.hosts.crypto import decrypt_secret

from . import registry
from .registry import BOOL, CHOICE, FLOAT, INT, SECRET, TIMEZONE

#: Seconds a cached snapshot is trusted in a process that did not write it.
CACHE_TTL_SECONDS = 5.0

_cache: dict = {"at": 0.0, "rows": None}


def invalidate() -> None:
    """Drop the cached snapshot. Called after any write, and by tests."""
    _cache["at"] = 0.0
    _cache["rows"] = None


def _rows() -> dict:
    now = time.monotonic()
    rows = _cache["rows"]
    if rows is not None and (now - _cache["at"]) < CACHE_TTL_SECONDS:
        return rows
    try:
        from .models import InstanceSetting
        rows = {r.key: r for r in InstanceSetting.objects.all()}
    except Exception:
        # No table yet (first migrate), or the database is unreachable. Either
        # way the environment and the code defaults still answer, and a
        # settings lookup must not be what takes the server down.
        rows = {}
    _cache["rows"] = rows
    _cache["at"] = now
    return rows


def env_locked(name: str) -> bool:
    """True when the environment sets *name* to a non-empty value.

    An empty variable — ``GREENBONE_URL=`` left in a compose file — is treated
    as unset, or the UI would be permanently disabled by a placeholder nobody
    meant as a value.
    """
    return bool(os.environ.get(name))


def _coerce(kind: str, raw: str, fallback):
    try:
        if kind == INT:
            return int(raw)
        if kind == FLOAT:
            return float(raw)
        if kind == BOOL:
            return raw.strip().lower() in ("true", "1", "yes", "on")
    except (TypeError, ValueError):
        return fallback
    return raw


def setting(name: str):
    """The effective value of *name*, honouring the precedence above."""
    key = registry.BY_NAME.get(name)
    fallback = getattr(django_settings, name, None)
    if key is None or env_locked(name):
        return fallback

    row = _rows().get(name)
    if row is None:
        return fallback

    if key.kind == SECRET:
        return decrypt_secret(row.value_encrypted) or fallback
    return _coerce(key.kind, row.value, fallback)


def is_set(name: str) -> bool:
    """True when *name* resolves to something an integration can use.

    Used by the UI to show "configured" without ever sending a secret back.
    """
    value = setting(name)
    return value not in (None, "", b"")


def source(name: str) -> str:
    """Where :func:`setting` got its answer: ``env``, ``db`` or ``default``."""
    if env_locked(name):
        return "env"
    return "db" if name in _rows() else "default"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(name: str, raw) -> tuple[bool, str]:
    """Check a submitted value. Returns ``(ok, message)``.

    The timezone check earns its place: schedule windows and the reboot gate
    are evaluated in this zone, so a typo here does not produce a cosmetic
    bug, it produces maintenance windows that never open.
    """
    key = registry.BY_NAME.get(name)
    if key is None:
        return False, "unknown setting"
    text = "" if raw is None else str(raw).strip()

    if key.kind == INT:
        try:
            if int(text) < 0:
                return False, "must not be negative"
        except ValueError:
            return False, "must be a whole number"
    elif key.kind == FLOAT:
        try:
            if float(text) < 0:
                return False, "must not be negative"
        except ValueError:
            return False, "must be a number"
    elif key.kind == TIMEZONE:
        if text:
            try:
                zoneinfo.ZoneInfo(text)
            except Exception:
                return False, "not an IANA timezone name"
    elif key.kind == CHOICE:
        if text and text not in key.choices:
            return False, "must be one of " + ", ".join(key.choices)
    return True, ""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write(name: str, raw, user=None) -> None:
    """Persist one value, or delete the row when the value is cleared.

    Deleting on empty is what makes "revert to the default" expressible. It
    also means a stored row always holds something real, so :func:`setting`
    never has to decide whether an empty string was a value or an absence.
    """
    from apps.hosts.crypto import encrypt_secret

    from .models import InstanceSetting

    key = registry.BY_NAME[name]
    text = "" if raw is None else str(raw).strip()

    if key.kind == SECRET:
        if not text:
            # Empty means "leave it alone" for a secret the browser never
            # received in the first place; clearing goes through clear().
            return
        InstanceSetting.objects.update_or_create(
            key=name,
            defaults={"value": "", "value_encrypted": encrypt_secret(text),
                      "updated_by": user},
        )
        invalidate()
        return

    if key.kind == BOOL:
        text = "true" if str(raw).strip().lower() in ("true", "1", "yes", "on") else "false"
    elif not text:
        clear(name)
        return

    InstanceSetting.objects.update_or_create(
        key=name,
        defaults={"value": text, "value_encrypted": b"", "updated_by": user},
    )
    invalidate()


def clear(name: str) -> None:
    """Remove the stored row so *name* falls back to env or code default."""
    from .models import InstanceSetting
    InstanceSetting.objects.filter(key=name).delete()
    invalidate()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def mail_connection():
    """A mail connection built from the effective SMTP settings.

    Django reads ``EMAIL_*`` out of ``settings`` at send time, which would
    ignore anything stored here, so the connection is constructed explicitly.

    The backend deserves a word. Vigil defaults to the console backend, which
    means mail is printed rather than sent — the right default for a fresh
    install with no mail server. Once someone has entered an SMTP host in the
    UI they have said what they want, so the SMTP backend is selected for
    them; otherwise "I configured mail and nothing arrived" would be the
    expected outcome. An explicit ``EMAIL_BACKEND`` in the environment still
    wins, and so does the test runner's locmem backend, because neither has a
    stored host to trigger this.
    """
    backend = django_settings.EMAIL_BACKEND
    if not env_locked("EMAIL_BACKEND") and "EMAIL_HOST" in _rows():
        backend = "django.core.mail.backends.smtp.EmailBackend"
    return get_connection(
        backend=backend,
        host=setting("EMAIL_HOST"),
        port=setting("EMAIL_PORT"),
        username=setting("EMAIL_HOST_USER"),
        password=setting("EMAIL_HOST_PASSWORD"),
        use_tls=setting("EMAIL_USE_TLS"),
    )
