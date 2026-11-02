"""Jackil's own widgets, offered on Vigil's dashboards.

Jackil says what it can draw (``/api/v1/vigil/widgets/``) and Vigil adds the
result to the catalog under a **Jackil** group, keyed ``jackil:<kind>``. The
far end is someone else's server, so every failure here is an answer rather
than an exception: a helpdesk that is down must leave the dashboard standing,
with the tile saying so in plain sentences.

Two caches keep a slow or dead Jackil off every page view. The catalog is
held for five minutes because it changes when Jackil is upgraded and not more
often; :data:`DOWN_KEY` is a 30-second "stop calling" flag, set the moment a
call fails, so one unreachable helpdesk costs one slow request per half
minute rather than one per widget per refresh.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from django.core.cache import cache

from . import client

logger = logging.getLogger(__name__)

PREFIX = "jackil:"
CATALOG_KEY = "jackil:widget-catalog"
CATALOG_SECONDS = 300
DOWN_KEY = "jackil:down"
DOWN_SECONDS = 30

#: A remote kind as it appears in a dashboard layout. The prefix is what tells
#: Vigil's own registry and Jackil's apart, and the character class is what
#: keeps a posted kind from naming a path on someone else's server.
KIND_RE = re.compile(r"^jackil:[a-z][a-z0-9_]{0,39}$")

#: The shapes phase 03d knows how to draw. Anything else in an answer is
#: garbage, not a widget.
SHAPES = ("stat", "list", "table", "gauge", "series")

NOT_CONNECTED = "Jackil is not connected."
NOT_ANSWERING = "Jackil is not answering right now."
GONE = "This Jackil widget no longer exists."
UNDRAWABLE = "Jackil sent a widget Vigil could not draw."


def is_remote(kind) -> bool:
    return isinstance(kind, str) and KIND_RE.match(kind) is not None


def _is_down() -> bool:
    return bool(cache.get(DOWN_KEY))


def _mark_down() -> None:
    cache.set(DOWN_KEY, True, DOWN_SECONDS)


def _sizes(shape: str) -> dict:
    if shape in ("stat", "gauge"):
        return {"w": 3, "h": 2, "min_w": 2, "min_h": 2}
    return {"w": 6, "h": 4, "min_w": 3, "min_h": 3}


def _settings(raw) -> dict:
    """Jackil's setting declarations in Vigil's own shape.

    Jackil's ``choice`` carries ``[value, label]`` pairs; Vigil's carries
    ``options`` plus a ``labels`` map, because a form generator that renders
    the values alone shows "open" where an operator wants "Open tickets".
    A type Vigil cannot render is dropped rather than guessed at.
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name, field in raw.items():
        if not isinstance(field, dict) or not isinstance(name, str):
            continue
        kind = field.get("type")
        label = str(field.get("label", name))
        if kind == "choice":
            pairs = [p for p in (field.get("choices") or [])
                     if isinstance(p, (list, tuple)) and len(p) >= 2]
            options = [str(p[0]) for p in pairs]
            if not options:
                continue
            default = str(field.get("default", options[0]))
            out[name] = {
                "type": "choice",
                "label": label,
                "default": default if default in options else options[0],
                "options": options,
                "labels": {str(p[0]): str(p[1]) for p in pairs},
            }
        elif kind == "int":
            try:
                default = int(field.get("default", 0))
                lo = int(field["min"]) if field.get("min") is not None else None
                hi = int(field["max"]) if field.get("max") is not None else None
            except (TypeError, ValueError):
                continue
            entry = {"type": "int", "label": label, "default": default}
            if lo is not None:
                entry["min"] = lo
            if hi is not None:
                entry["max"] = hi
            out[name] = entry
    return out


def _entry(widget: dict) -> dict | None:
    kind = widget.get("kind")
    shape = widget.get("shape")
    if not isinstance(kind, str) or shape not in SHAPES:
        return None
    full = f"{PREFIX}{kind}"
    if not KIND_RE.match(full):
        return None
    return {
        "group": "jackil",
        "remote": True,
        "shape": shape,
        "label": str(widget.get("title", kind)),
        "description": str(widget.get("description", "")),
        **_sizes(shape),
        "settings": _settings(widget.get("settings")),
    }


def remote_catalog() -> dict[str, dict]:
    """What Jackil offers, keyed ``jackil:<kind>``, or {} when it offers nothing.

    Empty is the honest answer whenever Jackil is off or recently failing: an
    empty group simply does not appear in the rail, which reads better than a
    list of tiles that cannot load.
    """
    if not client.enabled() or _is_down():
        return {}
    catalog = cache.get(CATALOG_KEY)
    if catalog is None:
        try:
            payload = client._request("GET", "/vigil/widgets/",
                                      timeout=client.RELATED_TIMEOUT)
        except client.JackilError as exc:
            logger.info("Jackil widget catalog unavailable: %s", exc)
            _mark_down()
            return {}
        widgets = payload.get("widgets") if isinstance(payload, dict) else None
        catalog = {}
        for widget in widgets if isinstance(widgets, list) else []:
            if not isinstance(widget, dict):
                continue
            entry = _entry(widget)
            if entry is not None:
                catalog[f"{PREFIX}{widget['kind']}"] = entry
        cache.set(CATALOG_KEY, catalog, CATALOG_SECONDS)
    return catalog


def _params(catalog_entry: dict | None, raw) -> dict:
    """Only the settings the catalog declares for this widget, cleaned.

    A dashboard's stored settings are operator JSON going out in a query
    string to another server; undeclared keys and unknown choices do not go.
    """
    declared = (catalog_entry or {}).get("settings") or {}
    if not isinstance(raw, dict) or not declared:
        return {}
    params: dict[str, str] = {}
    for name, field in declared.items():
        value = raw.get(name, field.get("default"))
        if field["type"] == "choice":
            options = field.get("options", [])
            if value not in options:
                value = field["default"]
            params[name] = str(value)
        elif field["type"] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = int(field.get("default", 0))
            lo, hi = field.get("min"), field.get("max")
            if lo is not None and value < lo:
                value = lo
            if hi is not None and value > hi:
                value = hi
            params[name] = str(value)
    return params


def _stat(label: str) -> dict:
    """The one shape every tile can fall back to.

    Anything that could not be fetched still has to occupy the grid slot it
    was saved into, and a stat tile is the smallest thing that can hold a
    sentence.
    """
    return {"version": 1, "shape": "stat",
            "data": {"value": 0, "label": label, "tone": "muted"}}


def remote_widget(kind: str, raw: dict) -> dict:
    """One Jackil widget's data, always as a ``{version, shape, data}`` envelope.

    Never raises. A tile that cannot load shows why where it would have shown
    a number; the layout the user saved is not thrown away because the
    helpdesk is having a bad afternoon.
    """
    if not client.enabled():
        return _stat(NOT_CONNECTED)
    if _is_down():
        return _stat(NOT_ANSWERING)

    # Only from the registry of record. An empty catalog is itself an answer —
    # "ask for nothing" — and must not be read as "let Jackil default", which
    # would put a second call in front of a helpdesk we just marked unreachable.
    catalog = remote_catalog()
    if _is_down():
        return _stat(NOT_ANSWERING)
    params = _params(catalog.get(kind), raw)
    short = kind.removeprefix(PREFIX)
    path = f"/vigil/widgets/{short}/"
    if params:
        path += "?" + urlencode(params)
    try:
        payload = client._request("GET", path, timeout=client.RELATED_TIMEOUT)
    except client.JackilNotFound:
        return _stat(GONE)
    except client.JackilError as exc:
        logger.info("Jackil widget %s unavailable: %s", short, exc)
        _mark_down()
        return _stat(NOT_ANSWERING)

    if not isinstance(payload, dict):
        return _stat(UNDRAWABLE)
    shape = payload.get("shape")
    data = payload.get("data")
    if shape not in SHAPES or not isinstance(data, dict):
        logger.info("Jackil widget %s sent shape %r Vigil cannot draw", short, shape)
        return _stat(UNDRAWABLE)
    return {"version": 1, "shape": shape, "data": data}
