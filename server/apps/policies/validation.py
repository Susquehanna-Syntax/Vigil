"""Validate a policy payload before anything is written.

Every refusal is a plain sentence for a 400 ``{"detail"}``. A policy that
would compile to a task the spec validator refuses is caught here instead, so
the operator sees the mistake on the policy screen and not at 2 a.m.
"""

from __future__ import annotations

import re
from typing import Any

from apps.tasks.spec import (_APP_ID_RE, _APP_SOURCE_CHOICES,
                             _APP_SOURCES_WITHOUT_VERSION, _APP_VERSION_RE)

from .models import WINDOWS_CLASSIFICATIONS, AppRule, UpdatePolicy

_CRON_RE = re.compile(r"^[0-9*,/-]{1,64}$")


class PolicyError(ValueError):
    pass


def _site_exists(site_id) -> bool:
    from vigil.scoping import _sites_models
    models = _sites_models()
    if models is None:
        return False
    return models.Site.objects.filter(pk=site_id).exists()


def clean_rules(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PolicyError("app_rules must be a list")
    states = set(AppRule.State.values)
    seen: set[tuple[str, str]] = set()
    rules = []
    for n, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise PolicyError(f"app rule #{n} must be an object")
        app = str(entry.get("app") or "").strip()
        source = str(entry.get("source") or "").strip()
        state = str(entry.get("state") or "").strip()
        version = str(entry.get("version") or "").strip()
        if not _APP_ID_RE.fullmatch(app):
            raise PolicyError(f"app rule #{n}: {app!r} is not an inventory id")
        if source and source not in _APP_SOURCE_CHOICES:
            raise PolicyError(f"app rule #{n}: unknown source {source!r}")
        if state not in states:
            raise PolicyError(f"app rule #{n}: state must be one of "
                              f"{', '.join(sorted(states))}")
        if state == AppRule.State.PINNED:
            if not version:
                raise PolicyError(f"app rule #{n}: a pinned app needs a version")
            if not _APP_VERSION_RE.fullmatch(version):
                raise PolicyError(f"app rule #{n}: {version!r} is not a version")
            if source in _APP_SOURCES_WITHOUT_VERSION:
                raise PolicyError(f"app rule #{n}: {source} cannot install a "
                                  f"specific version, so it cannot be pinned")
        elif version:
            raise PolicyError(f"app rule #{n}: only a pinned app takes a version")
        if (app, source) in seen:
            raise PolicyError(f"app rule #{n}: {app} ({source or 'any source'}) "
                              f"is listed twice")
        seen.add((app, source))
        rules.append({"app": app, "source": source, "state": state,
                      "version": version, "order": n - 1})
    return rules


def apply_fields(policy: UpdatePolicy, data: dict) -> None:
    """Set every field present in *data* on *policy*, refusing bad values."""
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise PolicyError("name is required")
        if len(name) > 120:
            raise PolicyError("name is longer than 120 characters")
        clash = UpdatePolicy.objects.filter(name=name)
        if policy.pk:
            clash = clash.exclude(pk=policy.pk)
        if clash.exists():
            raise PolicyError(f"a policy called {name!r} already exists")
        policy.name = name
    elif not policy.name:
        raise PolicyError("name is required")

    if "enabled" in data:
        policy.enabled = bool(data["enabled"])
    if "target_tags" in data:
        tags = data.get("target_tags") or []
        if not isinstance(tags, list):
            raise PolicyError("target_tags must be a list")
        policy.target_tags = [str(t).strip() for t in tags if str(t).strip()]
    if "site_id" in data:
        site_id = data.get("site_id") or None
        if site_id is not None:
            try:
                ok = _site_exists(site_id)
            except Exception:  # noqa: BLE001 — a malformed uuid is just unknown
                ok = False
            if not ok:
                raise PolicyError(f"unknown site {site_id!r}")
        policy.site_id = site_id

    for field in ("cron_minute", "cron_hour", "cron_dow"):
        if field in data:
            value = str(data.get(field) or "").strip()
            if not _CRON_RE.fullmatch(value):
                raise PolicyError(f"{field} {value!r} is not a cron field")
            setattr(policy, field, value)
    if "window_hours" in data:
        policy.window_hours = _int(data["window_hours"], "window_hours", 1, 24)
    if "wave_group_tag" in data:
        policy.wave_group_tag = str(data.get("wave_group_tag") or "").strip()[:120]
    if "approval_mode" in data:
        mode = data.get("approval_mode")
        if mode not in UpdatePolicy.ApprovalMode.values:
            raise PolicyError("approval_mode must be automatic or approve")
        policy.approval_mode = mode

    if "patch_enabled" in data:
        policy.patch_enabled = bool(data["patch_enabled"])
    if "windows_classifications" in data:
        raw = data.get("windows_classifications") or []
        if not isinstance(raw, list):
            raise PolicyError("windows_classifications must be a list")
        for c in raw:
            if c not in WINDOWS_CLASSIFICATIONS:
                raise PolicyError(f"unknown Windows Update classification {c!r}")
        policy.windows_classifications = list(dict.fromkeys(raw))
    if "deferral_days" in data:
        policy.deferral_days = _int(data["deferral_days"], "deferral_days", 0, 365)
    if "reboot" in data:
        if data.get("reboot") not in UpdatePolicy.Reboot.values:
            raise PolicyError("reboot must be never, in_window or ask")
        policy.reboot = data["reboot"]
    if "linux_updates" in data:
        if data.get("linux_updates") not in UpdatePolicy.LinuxUpdates.values:
            raise PolicyError("linux_updates must be security or all")
        policy.linux_updates = data["linux_updates"]


def _int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise PolicyError(f"{field} must be a number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PolicyError(f"{field} must be a number") from None
    if not low <= n <= high:
        raise PolicyError(f"{field} must be between {low} and {high}")
    return n
