"""YAML parsing and validation for TaskDefinition specs.

A task definition looks like::

    name: Restart nginx and verify
    description: Bounce nginx then hit the health endpoint
    relevance: web servers
    risk: standard
    actions:
      - id: bounce
        type: restart_service
        params: { service_name: nginx }
      - id: verify
        type: execute_script
        params: { script_name: healthcheck.sh }

The action ``type`` must be in :data:`ACTION_REGISTRY`, which mirrors the
agent-side executor. The server never emits raw commands; it only names
actions that the agent already knows how to run.
"""

from __future__ import annotations

import ast
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

import yaml

from ..expression import (
    ExprError,
    parse as expression_parse,
    referenced_inputs,
    referenced_steps,
)

# The action registry lives in registry.py; re-exported for existing importers.
from ..registry import (  # noqa: F401
    ACTION_REGISTRY,
    LEGACY_ACTION_ALIASES,
    action_outputs,
)
from ..scripthash import script_hash


class SpecError(ValueError):
    """Raised when a YAML task definition fails validation."""


_RISK_ORDER = {"low": 0, "standard": 1, "high": 2}

#: Ceiling for a per-step ``timeout:``, in seconds. Mirrors the agent's own
#: validation in executor._run_command — the two must agree, or the server
#: accepts a value the agent then rejects.
_MAX_STEP_TIMEOUT = 3600
_VALID_RISK = set(_RISK_ORDER)

#: How long a hunt stays open for hosts that never check in, in seconds —
#: used when a hunt step does not set its own ``stays_open``.
DEFAULT_STAYS_OPEN = 7 * 86400
_STAYS_OPEN_MIN = 3600
_STAYS_OPEN_MAX = 30 * 86400
_STAYS_OPEN_PATTERN = re.compile(r"^(\d+)([mhd])$")

_INPUT_TYPES = {"text", "choice", "boolean", "number"}
# Input references: ${{ inputs.foo }} (whitespace flexible). The ${{ }} marker is not valid
# bash, PowerShell or YAML, so script text can never be mistaken for an input. $${{ is an
# escaped literal ${{ — never a marker (the agent turns it back into ${{).
_INPUT_REF = re.compile(r"(?<!\$)\$\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# The pre-2026.13 form, {{ inputs.foo }} — accepted with a warning for one release.
_LEGACY_INPUT_REF = re.compile(r"(?<!\$)\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Step references: ${{ steps.<id>.status }} or ${{ steps.<id>.result.<field> }}.
# Either form, in one pattern, for single-pass substitution.
_ANY_INPUT_REF = re.compile(r"(?<!\$)(\$)?\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_STEPS_MARKER = re.compile(r"(?<!\$)\$\{\{\s*steps\.")
_STEP_REF = re.compile(
    r"(?<!\$)\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.(?:(status)|result\.([A-Za-z_][A-Za-z0-9_]*))\s*\}\}"
)
_INPUT_ID_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Metadata fields (optional, community-repo oriented). A task may name the
# CVEs it remediates, link the advisories it came from, and declare which
# OS families it applies to. None of them affect execution or risk — they
# exist so the Community tab and the vuln views can cross-reference.
_CVE_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)
_VALID_PLATFORMS = {"linux", "windows", "darwin"}

# Day-of-week aliases accepted in `schedule.window.days`. Stored canonically
# as 0..6 with 0 = Monday (matches Python's datetime.weekday()).
_DAY_ALIASES: dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_ALL_DAYS = list(range(7))


def _as_str(value: Any, field: str, max_len: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SpecError(f"{field!r} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise SpecError(f"{field!r} is too long (max {max_len})")
    return value


_SCRIPT_SHELLS = ("bash", "sh", "powershell", "pwsh")
_SCRIPT_MAX_LEN = 65536


_APP_SOURCE_CHOICES = frozenset({
    "dpkg", "rpm", "apk", "pacman", "flatpak", "snap",
    "winget", "chocolatey", "scoop", "registry",
})

#: An app's inventory id — what the Apps page shows as `package_id`. The
#: leading character is restricted to a letter, digit or `{` so an option
#: (`-oProxy=x`) and a marker cannot start it; `-`, `:`, `@` and `/` are
#: allowed inside (`libc6:amd64`, `org.mozilla.firefox`, an MSIX guid).
_APP_ID_RE = re.compile(r"^[A-Za-z0-9{][A-Za-z0-9._+:@/{}~-]{0,199}$")

#: A version pin (`2.43-2ubuntu2.4`, `1:3.0.13-1`). Same leading-character rule.
_APP_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,79}$")

#: Sources whose install command has no version argument at all.
_APP_SOURCES_WITHOUT_VERSION = frozenset({"snap", "flatpak", "registry"})


_REGISTRY_KEY_RE = re.compile(r"^[^\\\x00-\x1f]{1,255}$")

#: `app_install_custom` fetches over https only — the hash is checked, but a
#: plain-http installer on a network an attacker shares is not worth that.
_CUSTOM_URL_SCHEME = "https://"
_CUSTOM_URL_MAX_LEN = 2000
_CUSTOM_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Each installer kind is run by the one command that installs it silently, so
#: the kind is a real safety decision and not a label: `msi` never runs the file
#: directly, `deb`/`rpm` never run `dpkg`/`dnf` for a file with another
#: extension. The file extension is used only when the task does not name one.
_CUSTOM_KINDS = ("msi", "exe", "deb", "rpm")
#: A silent-install switch list — `/S`, `/quiet /norestart`,
#: `/qn /norestart /l*v C:\log.txt`. `"` is allowed so a quoted switch *value*
#: can be passed; the agent splits on spaces with no shell, so no shell
#: metacharacter can be interpreted, and that is why `$`, backtick, `;`, `|`,
#: `&`, `<`, `>` and `\` stay out.
_CUSTOM_ARGS_RE = re.compile(r'^[A-Za-z0-9 /=_.:,@+"-]{0,300}$')


_STACK_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,400}$")
_COMPOSE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


_APP_ENSURE_STATES = frozenset({"present", "latest", "pinned", "absent"})


# ── if/then/else branches ──────────────────────────────────────────────────────
#
# A task's ``actions:`` may branch on an earlier step's result. The server
# flattens the whole tree into one ordered list of leaf steps (``actions``
# stays a flat list, as every consumer expects) plus a ``flow`` tree that
# records the branches; phase 06 makes the agent follow it. Until then a
# branching task must never reach an agent (the check-in feature gate).

_MAX_BRANCH_DEPTH = 3
_MAX_BRANCH_LEAVES = 50

#: Values for a playbook's inputs, carried by a ``type: playbook`` action.
#: Scalars only — the playbook coerces them against its own declarations.
MAX_ACTION_INPUTS = 16
MAX_ACTION_INPUT_LEN = 500


#: What steps.<id>.status can be — the agent's StepResult.state.
STEP_STATUSES = ("ok", "error", "skipped")


# ── relevant: ──────────────────────────────────────────────────────────────────
#
# A task may declare when it applies to a host as a tree of hunt probes.
# The server parses, validates and normalises it at save time, substitutes
# inputs at deploy, and signs it into the task params; the agent evaluates
# it (phase 04).

_RELEVANT_OPS = ("all", "any", "not")
_MAX_RELEVANT_DEPTH = 3
_MAX_RELEVANT_PROBES = 10


_DETECTION_SEVERITIES = ("critical", "high", "medium", "low")
_MAX_BOOST_PROBES = 5




# Every name here, private ones included, for the other modules' star imports.
__all__ = ["ACTION_REGISTRY", "Any", "DEFAULT_STAYS_OPEN", "ExprError", "LEGACY_ACTION_ALIASES", "MAX_ACTION_INPUTS", "MAX_ACTION_INPUT_LEN", "STEP_STATUSES", "SpecError", "_ALL_DAYS", "_ANY_INPUT_REF", "_APP_ENSURE_STATES", "_APP_ID_RE", "_APP_SOURCES_WITHOUT_VERSION", "_APP_SOURCE_CHOICES", "_APP_VERSION_RE", "_COMPOSE_FILE_RE", "_CUSTOM_ARGS_RE", "_CUSTOM_KINDS", "_CUSTOM_SHA256_RE", "_CUSTOM_URL_MAX_LEN", "_CUSTOM_URL_SCHEME", "_CVE_PATTERN", "_DAY_ALIASES", "_DETECTION_SEVERITIES", "_INPUT_ID_PATTERN", "_INPUT_REF", "_INPUT_TYPES", "_ISO_DATE_PATTERN", "_LEGACY_INPUT_REF", "_MAX_BOOST_PROBES", "_MAX_BRANCH_DEPTH", "_MAX_BRANCH_LEAVES", "_MAX_RELEVANT_DEPTH", "_MAX_RELEVANT_PROBES", "_MAX_STEP_TIMEOUT", "_REGISTRY_KEY_RE", "_RELEVANT_OPS", "_RISK_ORDER", "_SCRIPT_MAX_LEN", "_SCRIPT_SHELLS", "_STACK_DIR_RE", "_STAYS_OPEN_MAX", "_STAYS_OPEN_MIN", "_STAYS_OPEN_PATTERN", "_STEPS_MARKER", "_STEP_REF", "_UUID_RE", "_VALID_PLATFORMS", "_VALID_RISK", "_as_str", "action_outputs", "annotations", "ast", "datetime", "expression_parse", "re", "referenced_inputs", "referenced_steps", "script_hash", "timedelta", "urllib", "yaml"]
