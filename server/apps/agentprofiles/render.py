"""Turning profile fields into a validated ``agent.yml``, and into YAML text.

Every limit here is the agent's own (``agent/vigil_agent/config.py``): the
point of validating on the way out is that an installer writes a file the agent
starts up on, rather than one it refuses to boot with.
"""
from __future__ import annotations

import re
import uuid

import yaml

from apps.relays.models import Relay
from apps.tasks.refusals import AGENT_ACTIONS

from .models import AgentProfile

#: Editable profile fields, in the order the wizard shows them.
FIELDS = (
    "name", "description", "mode", "allowlist", "primary_relay",
    "fallback_relays", "include_dashboard_fallback", "allow_remote_shell",
    "allow_remote_desktop", "remote_shell_user", "remote_desktop_port",
    "allow_reprovision", "tags", "process_watch", "checkin_interval",
    "docker_check_interval", "software_interval",
)

#: ``agent/vigil_agent/config.py``: the actions an allowlist can never grant.
NEVER_ALLOWLISTABLE = frozenset({
    "run_command", "reprovision_stage", "reprovision_commit",
    "reprovision_cleanup",
})

_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

_MODES = frozenset(AgentProfile.MODES)


def _url_ok(url: str, insecure: bool) -> str | None:
    """Error for an address an agent would send its token to, else None."""
    if not url.startswith(("https://", "http://")):
        return "must start with https://"
    if url.startswith("http://") and not insecure:
        return "is plain http but the dashboard is https — an agent sends its " \
               "token to every address it tries"
    return None


def _as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _validate_relays(data, errors: dict) -> tuple[list[str], list[str]]:
    """(primary id, fallback ids), with any complaint in *errors*."""
    primary = data.get("primary_relay")
    primary_id = str(primary) if primary else ""
    if primary_id and _as_uuid(primary_id) is None:
        errors["primary_relay"] = "must be a relay id"
        return "", []
    raw_fallbacks = data.get("fallback_relays")
    if raw_fallbacks is None:
        raw_fallbacks = []
    if not isinstance(raw_fallbacks, list):
        errors["fallback_relays"] = "must be a list of relay ids"
        return primary_id, []

    wanted = [str(r) for r in raw_fallbacks]
    if len(wanted) > 10:
        errors["fallback_relays"] = "at most 10 fallback relays"
        return primary_id, []
    if len(set(wanted)) != len(wanted):
        errors["fallback_relays"] = "lists the same relay twice"
        return primary_id, []
    if any(_as_uuid(rid) is None for rid in wanted):
        errors["fallback_relays"] = "must be a list of relay ids"
        return primary_id, []

    by_id = {str(r.id): r for r in Relay.objects.filter(
        pk__in=[rid for rid in [primary_id, *wanted] if rid])}

    ids: list[str] = []
    if primary_id:
        relay = by_id.get(primary_id)
        if relay is None or relay.kind != Relay.Kind.REMOTE:
            errors["primary_relay"] = "is not a known remote relay"
        elif not relay.enabled:
            errors["primary_relay"] = f"({relay.name}) is disabled"
        else:
            err = _url_ok(relay.url, insecure=False)
            if err or not relay.url:
                errors["primary_relay"] = f"({relay.name}) has no usable url"

    for rid in wanted:
        relay = by_id.get(rid)
        if relay is None or relay.kind != Relay.Kind.REMOTE:
            errors["fallback_relays"] = "names a relay that does not exist"
            break
        if not relay.enabled:
            errors["fallback_relays"] = f"includes the disabled relay {relay.name}"
            break
        if rid == primary_id:
            errors["fallback_relays"] = "includes the primary relay"
            break
        if _url_ok(relay.url, insecure=False) or not relay.url:
            errors["fallback_relays"] = f"{relay.name} has no usable url"
            break
        ids.append(rid)

    return primary_id, ids

def validate(data: dict) -> tuple[dict, dict]:
    """(clean, errors) for profile-shaped input.

    *errors* is empty when the input is usable; *clean* holds every field with
    its normalised value, so a caller writes what it gets back.
    """
    errors: dict = {}
    clean: dict = {}
    notes: list[str] = []

    name = str(data.get("name") or "").strip()
    if not name:
        errors["name"] = "is required"
    elif len(name) > 80:
        errors["name"] = "must be at most 80 characters"
    clean["name"] = name

    description = str(data.get("description") or "").strip()
    if len(description) > 300:
        errors["description"] = "must be at most 300 characters"
    clean["description"] = description

    mode = data.get("mode") or "monitor"
    if mode not in _MODES:
        errors["mode"] = f"must be one of {', '.join(AgentProfile.MODES)}"
        mode = "monitor"
    clean["mode"] = str(mode)

    raw_allow = data.get("allowlist")
    if raw_allow is None:
        raw_allow = []
    if not isinstance(raw_allow, list) or any(not isinstance(a, str) for a in raw_allow):
        errors["allowlist"] = "must be a list of action names"
        allow: list[str] = []
    else:
        allow, bad, forbidden = [], [], []
        for action in raw_allow:
            if action not in AGENT_ACTIONS:
                bad.append(action)
            elif action in NEVER_ALLOWLISTABLE:
                forbidden.append(action)
        if bad:
            errors["allowlist"] = f"unknown actions: {', '.join(sorted(bad))}"
        if forbidden:
            errors["allowlist"] = (
                "cannot be allowlisted: " + ", ".join(sorted(forbidden)))
        # De-duplicated and sorted so two profiles saying the same thing
        # render the same bytes.
        allow = sorted(set(raw_allow) - set(bad) - set(forbidden))
    clean["allowlist"] = allow

    primary_id, fallback_ids = _validate_relays(data, errors)
    clean["primary_relay"] = primary_id or None
    clean["fallback_relays"] = fallback_ids
    clean["include_dashboard_fallback"] = bool(data.get(
        "include_dashboard_fallback", True))

    user = str(data.get("remote_shell_user") or "root")
    if not _USER_RE.match(user):
        errors["remote_shell_user"] = (
            "must be 1-32 characters: lower-case letters, digits, '-' or '_', "
            "starting with a letter or underscore")
    clean["remote_shell_user"] = user

    port, err = _int(data.get("remote_desktop_port", 3389),
                     "remote_desktop_port", 1, 65535)
    if err:
        errors["remote_desktop_port"] = err
    clean["remote_desktop_port"] = port

    for field, low in (("checkin_interval", 10), ("docker_check_interval", 300),
                       ("software_interval", 900)):
        value, err = _int(data.get(field), field, low, 86_400 * 30)
        if err:
            errors[field] = err
        clean[field] = value

    tags, err = _strings(data.get("tags"), "tags", maximum=20, max_len=64)
    if err is None:
        for tag in tags:
            if not _TAG_RE.match(tag):
                err = (f"must be 1-64 characters of letters, digits, "
                       f"'_', '.', ':' or '-' — {tag!r} is not")
                break
    if err:
        errors["tags"] = err
    clean["tags"] = tags

    watched, err = _strings(data.get("process_watch"), "process_watch",
                            maximum=20, max_len=120)
    if err:
        errors["process_watch"] = err
    clean["process_watch"] = watched

    flags = {}
    for field in ("allow_remote_shell", "allow_remote_desktop",
                  "allow_reprovision"):
        flags[field] = bool(data.get(field, False))
        if clean["mode"] == "monitor" and flags[field]:
            # A monitor agent executes nothing, so the switch would be a lie
            # in the rendered file. Say so rather than quietly dropping it.
            notes.append(f"{field} was turned off: a monitor-mode agent runs "
                         f"nothing, so the switch would have no effect")
            flags[field] = False
        clean[field] = flags[field]

    if notes:
        clean["_notes"] = notes
    return clean, errors


def _int(value, field: str, low: int, high: int) -> tuple[int, str | None]:
    if value is None or value == "":
        value = {"checkin_interval": 60, "docker_check_interval": 21600,
                 "software_interval": 21600,
                 "remote_desktop_port": 3389}.get(field, low)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low, f"{field} must be a whole number"
    if not low <= number <= high:
        return number, f"{field} must be between {low} and {high}"
    return number, None


def _strings(value, field: str, maximum: int, max_len: int):
    if value is None:
        return [], None
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return [], "must be a list of strings"
    items = []
    for item in value:
        text = item.strip()
        if not text:
            continue
        if len(text) > max_len:
            return items, f"holds an entry longer than {max_len} characters"
        if text not in items:
            items.append(text)
    if len(items) > maximum:
        return items, f"holds at most {maximum} entries"
    return items, None


def agent_settings(clean: dict, dashboard_url: str) -> dict:
    """The ``agent.yml`` keys for validated profile fields."""
    dashboard = dashboard_url.rstrip("/")
    primary = None
    if clean.get("primary_relay"):
        primary = Relay.objects.filter(pk=clean["primary_relay"]).first()

    settings_out: dict = {}
    settings_out["server_url"] = primary.url if primary else dashboard

    fallbacks = [url for url in (
        Relay.objects.filter(pk=rid).values_list("url", flat=True).first()
        for rid in clean.get("fallback_relays") or []) if url]
    # The agent refuses a plain-http fallback under an https server_url (it
    # would send its token in the clear), so an http dashboard behind an https
    # relay is left out rather than written into a file the agent won't boot.
    if primary and clean.get("include_dashboard_fallback", True) and not (
            dashboard.startswith("http://") and primary.url.startswith("https://")):
        fallbacks.append(dashboard)
    settings_out["fallback_urls"] = [f for f in fallbacks if f != settings_out["server_url"]]

    settings_out["mode"] = clean["mode"]
    if clean["mode"] == "managed":
        # Meaningless in the other two modes, and a monitor agent that reports
        # an allowlist makes an operator believe it runs tasks.
        settings_out["allowlist"] = list(clean.get("allowlist") or [])
    for field in ("allow_remote_shell", "allow_remote_desktop",
                  "remote_shell_user", "remote_desktop_port",
                  "allow_reprovision"):
        settings_out[field] = clean[field]

    tags = list(clean.get("tags") or [])
    if tags:
        settings_out["tags"] = tags
    watched = list(clean.get("process_watch") or [])
    if watched:
        settings_out["process_watch"] = watched
    for field in ("checkin_interval", "docker_check_interval",
                  "software_interval"):
        settings_out[field] = clean[field]
    return settings_out


HEADER = ("# Vigil agent config — written by the installer from an enroll "
          "profile. This file is now the authority.")

_TOKEN_PLACEHOLDER = ("agent_token", "REPLACE_WITH_TOKEN")


def to_yaml(settings: dict) -> str:
    """A complete, writable ``agent.yml``."""
    body = yaml.safe_dump(settings, sort_keys=False, default_flow_style=False)
    out = [HEADER, *body.splitlines()]
    # The token placeholder goes right after server_url. Doing it on the
    # dumped text keeps the key order the settings dict declared.
    for i, line in enumerate(out):
        if line.startswith("server_url:"):
            out.insert(i + 1, f'agent_token: "{_TOKEN_PLACEHOLDER[1]}"')
            break
    return "\n".join(out) + "\n"
