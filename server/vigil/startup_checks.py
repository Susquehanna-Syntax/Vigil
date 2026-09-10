"""Configuration validation that runs once, at startup, and says everything.

The compose file's header calls four environment variables required, and then
supplies a default for every one of them. Skipping one therefore did not
produce an error — it produced a silent downgrade:

- no ``DJANGO_SECRET_KEY`` and the stack came up fully working, signing every
  session with a constant printed in the public repository;
- no ``VIGIL_SIGNING_KEY_SEED`` and the container crash-looped, with the only
  explanation a docstring in ``apps/hosts/apps.py``;
- no ``DJANGO_ALLOWED_HOSTS`` or ``VIGIL_PUBLIC_URL`` and the dashboard
  answered 400 on the machine's own name, with nothing to say which of five
  variables would fix it.

Each of those is a day-one failure for someone who has never seen this code.
This module turns all three into one message, printed before the server
starts, naming every problem at once rather than dying on whichever happens to
be checked first — a person fixing their compose file wants the whole list, not
the first item of it.
"""

from __future__ import annotations

import os
import sys

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

#: The value ``settings.py`` falls back to, and the one the shipped compose
#: file substitutes. Either means nobody set a key.
INSECURE_SECRET_KEYS = frozenset({
    "insecure-dev-key-change-me-in-production",
    "insecure-change-me",
})

_GENERATE_SECRET = (
    'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"')
_GENERATE_SEED = (
    'Generate one with: python3 -c "import os,base64; '
    'print(base64.b64encode(os.urandom(32)).decode())"')


#: Management commands that inspect or prepare the project rather than serve
#: it. A missing public URL is not their problem, and failing them would make
#: `manage.py check` — this project's build gate — a configuration test.
#:
#: Serving is what matters: gunicorn imports the WSGI application, which calls
#: django.setup(), which reaches this. So a misconfigured deployment still
#: fails loudly at the moment it would otherwise start answering requests, and
#: the operator still sees the whole list in `docker compose logs`.
NON_SERVING_COMMANDS = frozenset({
    "check", "test", "migrate", "makemigrations", "showmigrations",
    "sqlmigrate", "collectstatic", "createsuperuser", "changepassword",
    "shell", "dbshell", "diffsettings", "loaddata", "dumpdata",
})


def _is_non_serving_command() -> bool:
    """True when this process will never answer an HTTP request."""
    if os.environ.get("VIGIL_TESTING") == "1":
        return True
    # sys.argv[0] is manage.py; argv[1] is the subcommand, when there is one.
    return len(sys.argv) > 1 and sys.argv[1] in NON_SERVING_COMMANDS


def collect_problems() -> list[str]:
    """Every configuration problem that should stop this instance starting.

    Returns a list rather than raising so the caller can report all of them
    together. Empty list means the configuration is fit to serve.
    """
    problems: list[str] = []

    if settings.DEBUG:
        # Every check below is about a production posture. With DEBUG on the
        # operator has already said this is not one.
        return problems

    if settings.SECRET_KEY in INSECURE_SECRET_KEYS:
        problems.append(
            "DJANGO_SECRET_KEY is unset, so Vigil is using a value that is "
            "published in its own public repository. Every session cookie and "
            "password-reset token on this instance is forgeable by anyone who "
            "has read it. " + _GENERATE_SECRET)

    if not os.environ.get("VIGIL_SIGNING_KEY_SEED", "").strip():
        problems.append(
            "VIGIL_SIGNING_KEY_SEED is unset. It signs every task Vigil sends "
            "to an agent, and agents refuse unsigned tasks, so nothing will "
            "execute anywhere. " + _GENERATE_SEED)

    if not (os.environ.get("VIGIL_PUBLIC_URL", "").strip()
            or os.environ.get("DJANGO_ALLOWED_HOSTS", "").strip()):
        problems.append(
            "Neither VIGIL_PUBLIC_URL nor DJANGO_ALLOWED_HOSTS is set, so "
            "Vigil will answer only to 'localhost' and reject every other "
            "address with a bare 400 — including the name you will actually "
            "browse to. Set VIGIL_PUBLIC_URL to the URL you reach Vigil at, "
            "e.g. VIGIL_PUBLIC_URL=http://vigil.example.com:8000")

    return problems


def validate_or_die() -> None:
    """Raise once, listing everything wrong, or return quietly.

    ``VIGIL_SKIP_STARTUP_CHECKS=1`` bypasses this — it exists for build steps
    like ``collectstatic`` that run without the production environment.
    """
    if (os.environ.get("VIGIL_SKIP_STARTUP_CHECKS") == "1"
            or _is_non_serving_command()):
        return

    problems = collect_problems()
    if not problems:
        return

    numbered = "\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
    raise ImproperlyConfigured(
        f"\n\nVigil cannot start: {len(problems)} configuration "
        f"problem(s).\n\n{numbered}\n\n"
        "Set these in your .env file, or in Portainer's Environment variables "
        "tab, and start the stack again.\n")
