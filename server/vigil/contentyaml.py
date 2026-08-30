"""The community repo's YAML dialect, shared by every content type.

Vigil's own storage is relational: a baseline holds foreign keys to task
definitions, an automation holds a foreign key to a baseline. The community
repo has no database and no primary keys, so it references content the only
way a directory of files can — by **slug**, the filename without its
extension. ``task: install-nginx`` means ``tasks/install-nginx.yaml``.

Everything in this module exists to cross that gap in both directions, and to
do it in one place so the export and the import cannot disagree about what a
slug is. The rules here mirror ``common.py`` and the schema docs in
Susquehanna-Syntax/Vigil-Approved-Scripts; changing one without the other
produces files that pass here and fail the repo's CI, which is the failure
mode this module is shaped to prevent.
"""

from __future__ import annotations

import re
from typing import Any

import yaml


class ContentYamlError(ValueError):
    """Raised when a community YAML document fails validation.

    Deliberately a ``ValueError`` subclass, like ``SpecError``, so the view
    layer's existing 400-on-ValueError handling covers it unchanged.
    """


#: Placeholder authors the community repo rejects. Mirrors ``common.py``.
PLACEHOLDER_AUTHORS = {"", "admin", "todo", "unknown", "none", "n/a", "tbd"}

_SLUG_MAX = 60


def slugify(name: str, fallback: str = "untitled") -> str:
    """The community repo's filename rule: lowercase, non-alphanumerics
    collapsed to a single hyphen, trimmed, at most 60 characters.

    This is the *only* implementation on the server side. The editor's
    JavaScript carries a copy for the filename it suggests before anything is
    submitted; ``test_community_yaml`` pins the two together.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:_SLUG_MAX].strip("-")
    return slug or fallback


def load_mapping(text: str, what: str) -> dict[str, Any]:
    """Parse *text* and insist it is a non-empty mapping."""
    if not text or not text.strip():
        raise ContentYamlError(f"The {what} YAML is empty.")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContentYamlError(f"Could not parse the YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContentYamlError(
            f"A {what} must be a YAML mapping of fields, not a "
            f"{type(raw).__name__}.")
    return raw


def require_str(raw: dict, field: str, what: str, *, max_len: int,
                required: bool = True, default: str = "") -> str:
    value = raw.get(field, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ContentYamlError(f"{what}: '{field}' must be text.")
    value = value.strip()
    if required and not value:
        raise ContentYamlError(f"{what}: '{field}' is required.")
    if len(value) > max_len:
        raise ContentYamlError(
            f"{what}: '{field}' is longer than {max_len} characters.")
    return value


def require_tag_list(raw: dict, field: str, what: str, *,
                     max_items: int = 32) -> list[str]:
    value = raw.get(field) or []
    if isinstance(value, str):
        # A single tag written unquoted is a common and harmless mistake.
        value = [value]
    if not isinstance(value, list):
        raise ContentYamlError(f"{what}: '{field}' must be a list of tags.")
    if len(value) > max_items:
        raise ContentYamlError(
            f"{what}: '{field}' holds more than {max_items} tags.")
    out = []
    for tag in value:
        if not isinstance(tag, str) or not tag.strip():
            raise ContentYamlError(f"{what}: every '{field}' entry must be text.")
        out.append(tag.strip())
    return out


def check_author(author: str, what: str) -> str:
    """The repo requires a real name. A file authored by 'Admin' tells a
    reviewer nothing about who to ask when it breaks."""
    if author.strip().lower() in PLACEHOLDER_AUTHORS:
        raise ContentYamlError(
            f"{what}: 'author' must be a real name — {author!r} is a "
            f"placeholder the community repo rejects.")
    return author.strip()


def dump(fields: dict[str, Any]) -> str:
    """Serialise in declaration order, with block-folded descriptions.

    ``sort_keys=False`` matters: a reviewer reading a diff on GitHub should
    see the same field order every contributor's file has, and alphabetical
    order would put ``author`` above ``name``.
    """
    text = yaml.dump(fields, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=76)
    return text.rstrip() + "\n"
