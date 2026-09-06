"""What a widget may be.

One registry, read by the API, so the catalog and the per-widget settings form
are generated rather than hand-maintained in two places. Sizes are Gridstack
grid units on a 12-column grid.
"""

#: The metric defaults name a category/metric pair the agent actually reports
#: (see MetricPoint rows: cpu/usage_percent, memory/usage_percent,
#: disk/usage_percent, network/bytes_sent). A default that names nothing renders
#: an empty window, which reads as a broken widget rather than an unconfigured
#: one.
#:
#: Setting types the phase-04 form generator knows how to render.
#: "host"   — a host picker      "metric"  — a metric picker
#: "choice" — one of `options`   "int"     — a bounded number
#: "text"   — a short string     "bool"    — a checkbox
#: "longtext" — a textarea       "playbook"/"definition" — a picker
#:
#: ``group`` places a widget in the Add-widget rail: fleet, health, work,
#: security, utility, business. ``feature`` names a licensed feature the widget
#: needs; reads stay open so an unlicensed instance can grey-and-preview it.
WIDGET_REGISTRY: dict[str, dict] = {
    "host_status_grid": {
        "group": "fleet",
        "label": "Host status grid",
        "description": "Every host, with status and tags. Searchable.",
        "w": 12, "h": 5, "min_w": 4, "min_h": 3,
        "settings": {
            "tag_filter": {"type": "text", "label": "Only hosts tagged", "default": ""},
            "show_search": {"type": "bool", "label": "Show the search box", "default": True},
        },
    },
    "alert_list": {
        "group": "fleet",
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
        "group": "fleet",
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
        "group": "health",
        "label": "Metric chart",
        "description": "One metric for one host over time.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "category": {"type": "text", "label": "Category", "default": "cpu"},
            "metric": {"type": "metric", "label": "Metric", "default": "usage_percent"},
            "range_hours": {"type": "int", "label": "Hours back", "min": 1,
                             "max": 720, "default": 24},
        },
    },
    "gauge": {
        "group": "health",
        "label": "Gauge",
        "description": "One metric's latest value as a ring.",
        "w": 3, "h": 3, "min_w": 2, "min_h": 2,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "category": {"type": "text", "label": "Category", "default": "cpu"},
            "metric": {"type": "metric", "label": "Metric", "default": "usage_percent"},
        },
    },
    "docker_containers": {
        "group": "work",
        "label": "Docker containers",
        "description": "Containers on one host, with state and image.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
        },
    },
    "top_processes": {
        "group": "health",
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
        "group": "work",
        "label": "Rollout progress",
        "description": "Running rollouts and the wave each one is on.",
        "w": 6, "h": 3, "min_w": 4, "min_h": 2,
        "settings": {},
    },
    "vuln_score": {
        "group": "security",
        "label": "Vulnerability score",
        "description": "Fleet score and its trend.",
        "w": 4, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {},
    },
    # ── Conversions of the fixed dashboard blocks ────────────────────────
    "inactive_hosts": {
        "group": "fleet",
        "label": "Inactive hosts",
        "description": "Offline and not seen for a long time — the fleet's forgotten corners.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {
            "days": {"type": "int", "label": "Quiet for at least (days)",
                      "min": 1, "max": 365, "default": 90},
        },
    },
    "pending_enrollments": {
        "group": "fleet",
        "label": "Pending enrollments",
        "description": "Agents waiting for approval, with approve and reject.",
        "w": 6, "h": 3, "min_w": 4, "min_h": 2,
        "settings": {},
    },
    # ── Fleet health ─────────────────────────────────────────────────────
    "reboot_required": {
        "group": "health",
        "label": "Awaiting reboot",
        "description": "Hosts holding a pending kernel or update reboot.",
        "w": 4, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {},
    },
    "outdated_agents": {
        "group": "health",
        "label": "Outdated agents",
        "description": "Hosts running a version older than this server ships.",
        "w": 4, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {},
    },
    "disk_pressure": {
        "group": "health",
        "label": "Disk pressure",
        "description": "The fullest disks across the fleet.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 25, "default": 8},
            "threshold": {"type": "int", "label": "Only above (%)", "min": 0,
                           "max": 100, "default": 0},
        },
    },
    "network_throughput": {
        "group": "health",
        "label": "Network throughput",
        "description": "Bytes in and out for one host, over time.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 3,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "range_hours": {"type": "int", "label": "Hours back", "min": 1,
                             "max": 720, "default": 24},
        },
    },
    # ── Work in flight ───────────────────────────────────────────────────
    "task_history": {
        "group": "work",
        "label": "Recent task runs",
        "description": "What has run lately, and how it went.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 2,
        "settings": {
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 50, "default": 10},
            "failures_only": {"type": "bool", "label": "Only failures", "default": False},
        },
    },
    "wave_status": {
        "group": "work",
        "label": "Waves",
        "description": "Each wave and how many machines it holds.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {
            "group_tag": {"type": "text", "label": "Only this wave group", "default": ""},
        },
    },
    "playbook_coverage": {
        "group": "work",
        "label": "Playbook coverage",
        "description": "Which targeted hosts have run a playbook, and which have not.",
        "w": 6, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {
            "playbook": {"type": "playbook", "label": "Playbook", "default": ""},
        },
    },
    "automation_activity": {
        "group": "work",
        "label": "Automations",
        "description": "Automations and when each last fired.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 25, "default": 8},
        },
    },
    # ── Security and compliance ──────────────────────────────────────────
    "vuln_findings": {
        "group": "security",
        "label": "Vulnerability findings",
        "description": "Open findings, worst first.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 2,
        "settings": {
            "severity": {"type": "choice", "label": "Minimum severity",
                          "options": ["low", "medium", "high", "critical"],
                          "default": "high"},
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 50, "default": 10},
        },
    },
    "firewall_status": {
        "group": "security",
        "label": "Firewall",
        "description": "One host's firewall backend and rule count.",
        "w": 4, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
        },
    },
    "windows_patches": {
        "group": "security",
        "label": "Windows patch compliance",
        "description": "Windows hosts and how many updates each is missing.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {},
    },
    # ── Utility ──────────────────────────────────────────────────────────
    "notes": {
        "group": "utility",
        "label": "Notes",
        "description": "A note for whoever is looking at this dashboard.",
        "w": 4, "h": 3, "min_w": 2, "min_h": 2,
        "settings": {
            "title": {"type": "text", "label": "Heading", "default": ""},
            "body": {"type": "longtext", "label": "Text", "default": ""},
        },
    },
    "quick_deploy": {
        "group": "utility",
        "label": "Quick deploy",
        "description": "One button that deploys a chosen task. Opens the usual confirmation.",
        "w": 3, "h": 2, "min_w": 2, "min_h": 2,
        "settings": {
            "definition": {"type": "definition", "label": "Task", "default": ""},
        },
    },
    "clock": {
        "group": "utility",
        "label": "Clock",
        "description": "The time somewhere, for a wall display.",
        "w": 3, "h": 2, "min_w": 2, "min_h": 2,
        "settings": {
            "timezone": {"type": "text", "label": "Timezone", "default": "UTC"},
            "label": {"type": "text", "label": "Caption", "default": ""},
        },
    },
    "uptime_bars": {
        "group": "utility",
        "label": "Uptime",
        "description": "Daily uptime bars for one host.",
        "w": 6, "h": 3, "min_w": 3, "min_h": 2,
        "settings": {
            "host": {"type": "host", "label": "Host", "default": ""},
            "days": {"type": "int", "label": "Days", "min": 7, "max": 90, "default": 30},
        },
    },
    # ── Business ─────────────────────────────────────────────────────────
    "site_summary": {
        "group": "business",
        "label": "Sites",
        "description": "Each site and the state of its machines.",
        "w": 6, "h": 4, "min_w": 3, "min_h": 2,
        "settings": {},
        "feature": "sites",
    },
    "audit_tail": {
        "group": "business",
        "label": "Audit log",
        "description": "The most recent audited actions.",
        "w": 6, "h": 4, "min_w": 4, "min_h": 2,
        "settings": {
            "limit": {"type": "int", "label": "How many", "min": 1, "max": 50, "default": 10},
        },
        "feature": "audit_log",
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
        elif field["type"] == "longtext":
            # A note is prose, not an identifier. Still bounded: this is stored
            # per widget and read on every dashboard load.
            value = str(value)[:4000]
        else:
            value = str(value)[:200]
        cleaned[name] = value
    return cleaned
