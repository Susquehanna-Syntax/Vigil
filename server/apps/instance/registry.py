"""The settings an operator may change from the UI instead of the .env file.

Vigil's deployment story is a compose file, and for a long time every
integration lived there: pointing Vigil at a Greenbone container meant editing
``.env`` and restarting the stack, and the Vulnerabilities page said so in as
many words. That is the right home for anything Django reads while it is
booting — database credentials, the signing seed, allowed hosts — but it is a
poor home for a scanner password or a retention threshold, which are read at
call time and change with how someone is using the product.

Only keys that are read *at call time* belong here. Anything consumed while
Django builds its configuration (INSTALLED_APPS, ALLOWED_HOSTS, the database,
the signing seed) must stay in the environment: a DB row cannot exist before
the database does, and letting the UI edit request-security settings would put
a privilege boundary inside the application it protects.

``VIGIL_METRIC_RETENTION_DAYS`` is deliberately absent. It is read by the
cleanup task *and* baked into a TimescaleDB retention policy at migrate time;
a UI that changed only the task would leave the two disagreeing silently.

Precedence follows CivilConfig: **an environment variable still wins when
set**, so a GitOps install is never fighting a row someone typed into a form.
The UI shows those keys read-only and says where the value came from.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Value kinds. ``SECRET`` is stored Fernet-encrypted and never sent to a
#: browser — the UI is told only whether one is set.
STR = "str"
SECRET = "secret"
INT = "int"
FLOAT = "float"
BOOL = "bool"
TIMEZONE = "timezone"
CHOICE = "choice"


@dataclass(frozen=True)
class Key:
    """One settable value.

    ``name`` is both the Django settings name and the environment variable
    name. They are identical for every key here, which is what lets a single
    name answer "is this locked by the environment?" and "what is the
    fallback?" without a second mapping to keep in sync.
    """

    name: str
    group: str
    kind: str
    label: str
    help: str = ""
    placeholder: str = ""
    choices: tuple[str, ...] = ()


#: Group id → (label, blurb). Order here is the order the UI renders.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("greenbone", "Greenbone / OpenVAS", (
        "Talks GMP over TLS to a Greenbone Community Edition stack you run "
        "yourself. The URL is the host:port of the GMP listener — 9390 for "
        "greenbone-community-container."
    )),
    ("nessus", "Nessus / Tenable", (
        "Uses Nessus API keys, not a username and password. Generate them "
        "under your Nessus user's API Keys tab."
    )),
    ("jackil", "Jackil (alert \u2192 ticket)", (
        "Jackil is the SQSY helpdesk. When an alert fires, Vigil opens a "
        "ticket in it and links the two, so the same alert never opens a "
        "second one; when the alert clears, Vigil posts a note on the ticket."
    )),
    ("email", "Email (SMTP)", (
        "Where alert notifications are sent from. Setting a host here also "
        "switches Vigil off the console backend, so mail actually leaves the "
        "server."
    )),
    ("retention", "Retention & thresholds", (
        "How long Vigil keeps history, and when it warns about its own "
        "database. Metric retention stays in .env — it is enforced by a "
        "TimescaleDB policy, not by a task."
    )),
    ("locale", "Timezone & time format", (
        "Used to evaluate schedule windows and to render times in the UI."
    )),
)

KEYS: tuple[Key, ...] = (
    # ── Greenbone / OpenVAS ──────────────────────────────────────────────
    Key("GREENBONE_URL", "greenbone", STR, "GMP host:port",
        "Host and port of the GMP listener.", "greenbone.lan:9390"),
    Key("GREENBONE_USERNAME", "greenbone", STR, "Username", "", "admin"),
    Key("GREENBONE_PASSWORD", "greenbone", SECRET, "Password"),
    Key("GREENBONE_VERIFY_SSL", "greenbone", BOOL, "Verify TLS certificate",
        "Greenbone ships a self-signed certificate; turn this off only on a "
        "network you trust."),
    Key("GREENBONE_PORT_LIST_ID", "greenbone", STR, "Port list UUID",
        "Blank uses the well-known “All IANA assigned TCP” list."),

    # ── Nessus ───────────────────────────────────────────────────────────
    Key("NESSUS_URL", "nessus", STR, "Server URL", "", "https://nessus.lan:8834"),
    Key("NESSUS_ACCESS_KEY", "nessus", SECRET, "Access key"),
    Key("NESSUS_SECRET_KEY", "nessus", SECRET, "Secret key"),
    Key("NESSUS_VERIFY_SSL", "nessus", BOOL, "Verify TLS certificate"),

    # ── Jackil ───────────────────────────────────────────────────────────
    Key("JACKIL_ENABLED", "jackil", BOOL, "Open tickets for alerts",
        "Off until you turn it on, even once the URL and key are set."),
    Key("JACKIL_URL", "jackil", STR, "Jackil URL", "", "https://help.example.com"),
    Key("JACKIL_API_KEY", "jackil", SECRET, "API key",
        "From Jackil: Console \u2192 API keys. The key's user must be an agent "
        "or admin."),
    Key("JACKIL_VERIFY_SSL", "jackil", BOOL, "Verify TLS certificate"),
    Key("JACKIL_MIN_SEVERITY", "jackil", CHOICE, "Open a ticket from",
        "A ticket per info-level alert turns the helpdesk queue into a metrics "
        "feed.", "", ("critical", "warning", "info")),
    Key("JACKIL_RESOLVE_ON_CLEAR", "jackil", BOOL, "Resolve the ticket when the alert clears",
        "Off leaves the ticket open with a note, for someone to close by hand."),
    Key("JACKIL_REQUESTER_EMAIL", "jackil", STR, "Requester address",
        "Set on each ticket so replies reach a monitored mailbox rather than "
        "nobody.", "noc@example.com"),
    Key("JACKIL_TAGS", "jackil", STR, "Tags",
        "Comma-separated, added to every ticket Vigil opens.", "vigil"),

    # ── Email ────────────────────────────────────────────────────────────
    Key("EMAIL_HOST", "email", STR, "SMTP host", "", "smtp.example.com"),
    Key("EMAIL_PORT", "email", INT, "Port", "", "587"),
    Key("EMAIL_HOST_USER", "email", STR, "Username", "", "vigil@example.com"),
    Key("EMAIL_HOST_PASSWORD", "email", SECRET, "Password"),
    Key("EMAIL_USE_TLS", "email", BOOL, "Use STARTTLS"),
    Key("VIGIL_NOTIFICATION_FROM_EMAIL", "email", STR, "From address",
        "", "Vigil <vigil@example.com>"),

    # ── Retention & thresholds ───────────────────────────────────────────
    Key("VIGIL_ALERT_RETENTION_DAYS", "retention", INT, "Alert history (days)",
        "Resolved alerts older than this are deleted."),
    Key("VIGIL_DB_SIZE_WARN_GB", "retention", FLOAT, "Database warning (GB)"),
    Key("VIGIL_DB_SIZE_CRIT_GB", "retention", FLOAT, "Database critical (GB)"),
    Key("VIGIL_TASK_EXPIRY_GRACE_SECONDS", "retention", INT,
        "Task expiry grace (seconds)",
        "How long a dispatched task may sit unanswered before it is marked "
        "expired."),
    Key("VIGIL_AI_TIMEOUT_SECONDS", "retention", INT, "AI request timeout (seconds)",
        "Local models on modest hardware are slow; this is generous on purpose."),
    Key("VIGIL_KEV_LIVE_REFRESH", "retention", BOOL,
        "Fetch the CISA KEV catalog live",
        "Off by default — Vigil ships a bundled snapshot and never reaches "
        "out on its own."),

    # ── Locale ───────────────────────────────────────────────────────────
    Key("VIGIL_TIMEZONE", "locale", TIMEZONE, "Server timezone",
        "An IANA name, e.g. America/New_York. Schedule windows are evaluated "
        "in this zone.", "UTC"),
    Key("VIGIL_TIME_FORMAT", "locale", CHOICE, "Time format", "", "",
        ("12h", "24h")),
)

BY_NAME: dict[str, Key] = {k.name: k for k in KEYS}

#: Keys whose value must never be sent to a browser or written to a log.
SECRET_NAMES = frozenset(k.name for k in KEYS if k.kind == SECRET)
