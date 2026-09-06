"""What a widget may be.

One registry, read by the API, so the catalog and the per-widget settings form
are generated rather than hand-maintained in two places. Sizes are Gridstack
grid units on a 12-column grid.
"""

#: Setting types the phase-04 form generator knows how to render.
#: "host"   — a host picker      "metric"  — a metric picker
#: "choice" — one of `options`   "int"     — a bounded number
#: "text"   — a short string     "bool"    — a checkbox
WIDGET_REGISTRY: dict[str, dict] = {
    "host_status_grid": {
        "label": "Host status grid",
        "description": "Every host as a card, with its status and tags.",
        "w": 12, "h": 5, "min_w": 4, "min_h": 3,
        "settings": {
            "tag_filter": {"type": "text", "label": "Only hosts tagged", "default": ""},
        },
    },
    "alert_list": {
        "label": "Alerts",
        "description": "Firing alerts, newest first.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {
            "severity": {"type": "choice", "label": "Minimum severity",
                          "options": ["info", "warning", "critical"],
                          "default": "warning"},
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 50,
                       "default": 10},
        },
    },
    "stat_tile": {
        "label": "Stat tile",
        "description": "One number: hosts, online, alerts firing, or pending.",
        "w": 3, "h": 2, "min_w": 2, "min_h": 2,
        "settings": {
            "stat": {"type": "choice", "label": "Which number",
                      "options": ["hosts", "online", "offline", "alerts_firing",
                                  "pending"],
                      "default": "hosts"},
        },
    },
    "metric_chart": {
        "label": "Metric chart",
        "description": "One metric for one host over time.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "category": {"type": "text", "label": "Category", "default": "system"},
            "metric": {"type": "metric", "label": "Metric", "default": "cpu_percent"},
            "range_hours": {"type": "int", "label": "Hours back", "min": 1,
                             "max": 720, "default": 24},
        },
    },
    "gauge": {
        "label": "Gauge",
        "description": "One metric's latest value as a ring.",
        "w": 3, "h": 3, "min_w": 2, "min_h": 2,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "category": {"type": "text", "label": "Category", "default": "system"},
            "metric": {"type": "metric", "label": "Metric", "default": "cpu_percent"},
        },
    },
    "docker_containers": {
        "label": "Docker containers",
        "description": "Containers on one host, with state and image.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
        },
    },
    "top_processes": {
        "label": "Top processes",
        "description": "Busiest processes on one host.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "sort_by": {"type": "choice", "label": "Sort by",
                         "options": ["cpu", "memory"], "default": "cpu"},
        },
    },
    "rollout_progress": {
        "label": "Rollout progress",
        "description": "Running rollouts and the wave each one is on.",
        "w": 6, "h": 3, "min_w": 4, "min_h": 2,
        "settings": {},
    },
    "vuln_score": {
        "label": "Vulnerability score",
        "description": "Fleet score and its trend.",
        "w": 4, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {},
    },
}

#: Grid width every layout is expressed against.
GRID_COLUMNS = 12

#: How tall one widget may be. A separate number from the column count: rows
#: and columns are different axes, and a chart two screens tall is a layout
#: nobody can read rather than one worth storing.
GRID_MAX_ROWS = 24


def default_settings(kind: str) -> dict:
    """The settings a freshly added widget of *kind* starts with."""
    spec = WIDGET_REGISTRY.get(kind) or {}
    return {name: field.get("default")
            for name, field in (spec.get("settings") or {}).items()}


def clean_settings(kind: str, raw) -> dict:
    """Keep only settings the registry declares for *kind*.

    A widget's settings are operator-supplied JSON. Storing unknown keys would
    let one widget's payload grow without bound and would leak whatever a
    client happened to send into every later read.
    """
    spec = WIDGET_REGISTRY.get(kind) or {}
    declared = spec.get("settings") or {}
    if not isinstance(raw, dict):
        return default_settings(kind)
    cleaned = default_settings(kind)
    for name, value in raw.items():
        if name not in declared:
            continue
        field = declared[name]
        if field["type"] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            lo, hi = field.get("min"), field.get("max")
            if lo is not None and value < lo:
                value = lo
            if hi is not None and value > hi:
                value = hi
        elif field["type"] == "choice":
            if value not in field.get("options", []):
                continue
        elif field["type"] == "bool":
            value = bool(value)
        else:
            value = str(value)[:200]
        cleaned[name] = value
    return cleaned
