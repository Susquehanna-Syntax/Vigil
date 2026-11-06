"""The three secrets a Vigil install needs, made on first boot.

Before this module an operator had to hand-make ``DJANGO_SECRET_KEY``,
``VIGIL_SIGNING_KEY_SEED`` and the database password before the stack would
start, and the seed had to be exactly 44 base64 characters. A skipped value was
either a crash loop or — worse — a working install signed with a key published
in this repository, and a forgotten database password was literally ``vigil``.

Here the values are *made* instead: the ``secrets`` service in the shipped
compose file runs :func:`ensure` once per boot and keeps the results in a named
volume the application containers read. An environment variable always wins, so
an install whose ``.env`` already carries secrets keeps using them.

**Standard library only, on purpose.** This is imported from ``settings.py`` —
before Django exists — and also run as ``python -m vigil.bootsecrets`` in a
container that never touches Django.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys

try:  # pwd is unavailable on some platforms (Windows dev checkouts).
    import pwd
except ImportError:  # pragma: no cover - platform dependent
    pwd = None

#: Secret name → the environment variable that overrides it. Iteration order is
#: the order ``ensure`` reports them in.
NAMES = {
    # file name          environment variable
    "django_secret_key": "DJANGO_SECRET_KEY",
    "signing_key_seed":  "VIGIL_SIGNING_KEY_SEED",
    "postgres_password": "POSTGRES_PASSWORD",
}

DEFAULT_DIR = "/app/secrets"

#: The password an install from before this phase gives its database when
#: ``POSTGRES_PASSWORD`` was never set. The volume outlives the setting, so a
#: regenerated password would lock the stack out of its own data.
LEGACY_POSTGRES_PASSWORD = "vigil"

#: The database image reads the password file as its own (non-root) user, so
#: this one secret cannot be mode 0600 owned by the application user.
_WORLD_READABLE = {"postgres_password"}


def secrets_dir() -> str:
    """Where the generated secrets live."""
    return os.environ.get("VIGIL_SECRETS_DIR", DEFAULT_DIR)


def value(name: str, default: str = "") -> str:
    """The configured value of a secret, or ``default``.

    Environment first, then the generated file, then ``default``. Never creates
    anything — generating is :func:`ensure`'s job, run by one container.
    """
    from_env = os.environ.get(NAMES[name], "")
    if from_env.strip():
        return from_env.strip()
    try:
        with open(os.path.join(secrets_dir(), name)) as handle:
            return handle.read().strip()
    except OSError:
        # A missing file is the normal case on a machine that hasn't booted
        # yet, and an unreadable one is a mis-mounted volume; neither is this
        # function's problem to report.
        return default


def generate(name: str) -> str:
    """A fresh value for a secret."""
    if name == "django_secret_key":
        return secrets.token_urlsafe(50)
    if name == "signing_key_seed":
        # Always 44 characters ending in '='; the seed is 32 raw bytes.
        return base64.b64encode(os.urandom(32)).decode()
    if name == "postgres_password":
        return secrets.token_urlsafe(32)
    raise ValueError(f"unknown secret: {name}")


def _has_existing_database(pgdata) -> bool:
    return pgdata is not None and os.path.exists(os.path.join(pgdata, "PG_VERSION"))


def _content(name: str, existing_pgdata: bool) -> tuple[str, str]:
    """The value to store for a not-yet-created secret, and how it was chosen."""
    from_env = os.environ.get(NAMES[name], "")
    if from_env.strip():
        return from_env.strip(), "adopted"
    if name == "postgres_password" and existing_pgdata:
        return LEGACY_POSTGRES_PASSWORD, "legacy"
    return generate(name), "generated"


def ensure(directory: str, pgdata=None, owner=None) -> dict[str, str]:
    """Make every missing secret in ``directory``; never touch an existing one.

    Returns ``{name: "kept" | "adopted" | "legacy" | "generated"}`` and prints
    one line per name. A secret value is never printed.
    """
    os.makedirs(directory, exist_ok=True)
    # 0711, not 0700: the volume root is this directory, and the database
    # (as ``postgres``) and the application (as ``vigil``, after dropping
    # root) must reach their files through it. Execute-only lets them open a
    # file they name without listing the directory; each secret's own mode
    # decides who may read it.
    os.chmod(directory, 0o711)
    existing_pgdata = _has_existing_database(pgdata)
    outcomes: dict[str, str] = {}

    for name in NAMES:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            outcomes[name] = "kept"
            print(f"{name}: kept")
            continue

        content, outcome = _content(name, existing_pgdata)
        mode = 0o644 if name in _WORLD_READABLE else 0o600
        try:
            # O_EXCL: two containers starting at once must not both write, and
            # whoever loses keeps the winner's value rather than overwriting it.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            outcomes[name] = "kept"
            print(f"{name}: kept")
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(content + "\n")
        if owner is not None and name not in _WORLD_READABLE:
            os.chown(path, owner[0], owner[1])
        outcomes[name] = outcome
        print(_reported(name, outcome))
        if outcome == "legacy":
            print("WARNING: the database still uses the old default password "
                  f"'{LEGACY_POSTGRES_PASSWORD}'. Set POSTGRES_PASSWORD and "
                  "change it in Postgres (ALTER USER) to replace it.")

    return outcomes


def _reported(name: str, outcome: str) -> str:
    """The operator-facing line for one secret. Never contains the value."""
    if outcome == "adopted":
        return f"{name}: adopted from the environment"
    if outcome == "legacy":
        return f"{name}: legacy default (existing database)"
    return f"{name}: {outcome}"


def _vigil_owner():
    """The uid/gid the application runs as, so it can read its own secrets."""
    if pwd is None:
        return None
    try:
        pw = pwd.getpwnam("vigil")
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def main(argv: list[str]) -> int:
    ensure(argv[1] if len(argv) > 1 else secrets_dir(),
           pgdata=os.environ.get("VIGIL_PGDATA") or None,
           owner=_vigil_owner())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
